import argparse, hashlib, hmac, ipaddress, json, os, re, secrets, shutil, socket, sqlite3, ssl, subprocess, sys, threading, time, unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import lru_cache
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from queue import Empty, Queue
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk  # noqa: F401
except ImportError:   # equipos sin Tkinter: el modo consola y el servidor de sensores siguen funcionando
    tk = filedialog = messagebox = simpledialog = ttk = None

import numpy as np
import requests
from scipy import signal
from scipy.io import wavfile

if str(Path(__file__).resolve().parent) not in sys.path:     # para encontrar clasificador_sonidos.py aunque se ejecute desde otra carpeta
    sys.path.insert(0, str(Path(__file__).resolve().parent))
try:   # red neuronal que escucha el audio (YAMNet en NumPy puro). Si falta el archivo del modelo, se usan solo las reglas.
    from clasificador_sonidos import ClasificadorSonidos, AjusteLocal
except Exception:   # pragma: no cover
    ClasificadorSonidos = AjusteLocal = None

RAIZ = Path(__file__).resolve().parent
DIR_BD = RAIZ / "base_de_datos"
DIR_AUDIOS = DIR_BD / "audios_entrada"
ARCHIVO_BD = DIR_BD / "monitoreo.db"
EXTENSIONES_AUDIO = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}

LMSTUDIO_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1").rstrip("/")
LMSTUDIO_MODELO = os.environ.get("LMSTUDIO_MODELO", "").strip()
LMSTUDIO_TIMEOUT_S = float(os.environ.get("LMSTUDIO_TIMEOUT_S", "150"))   # espera de lectura; la alerta ya salió antes
# (antes 25 s: con un modelo de 8B en CPU que tarda ~20 s por consulta, la segunda consulta en cola superaba el límite)
LMSTUDIO_CONTEXTO = int(os.environ.get("LMSTUDIO_CONTEXTO", "4096"))     # contexto pequeño: evita agotar la RAM
IA_MODO = os.environ.get("IA_MODO", "auto").strip().lower()              # auto = solo casos ambiguos | siempre
IA_PAUSA_TRAS_FALLO_S = 30.0
MAX_BYTES_SENSOR = 15 * 1024 * 1024
ARCHIVO_CONFIG = RAIZ / "configuracion.json"

FRECUENCIA_MUESTREO = 16000
DURACION_MAX_S = 60.0
SEGMENTO_S = 2.0
PASO_S = 1.0
FRAME_S = 0.025
HOP_S = 0.010

CATEGORIAS_AMENAZA = {"motosierra", "disparo", "incendio"}
UMBRAL_CONFIANZA_ALERTA = 0.60
KPI_TIEMPO_DETECCION_S = 2.0
KPI_TIEMPO_ALERTA_S = 10.0
KPI_PRECISION_MIN = 0.90
KPI_FALSAS_ALARMAS_MAX = 0.10

SENSOR_POR_DEFECTO = "SENSOR-DEMO"
SENSORES_CONOCIDOS = {
    "SENSOR-DEMO": (4.5990, -74.3130),
    "S01": (4.6010, -74.3150),
    "S02": (4.5975, -74.3105),
    "S03": (4.5950, -74.3160),
    "S04": (4.6030, -74.3110),
    "S05": (4.5965, -74.3185)
}
PUESTO_GUARDABOSQUES = ("Puesto de guardabosques", 4.5985, -74.3140)

EPS = 1e-12
MAX_PARADAS = 12
CATEGORIAS = ["motosierra", "disparo", "incendio", "motor_vehiculo", "ambiente_natural", "otro"]
RED_ACTIVA = os.environ.get("RED_NEURONAL", "1").strip() != "0"            # 0 = desactivar la red y usar solo reglas
RED_GANANCIA_MAX_DB = 30.0       # amplificación máxima para sonidos lejanos antes de la red
RED_NIVEL_OBJETIVO_DB = -25.0
NIVELES = ["ninguna", "baja", "media", "alta"]

DB_LOCK = threading.RLock()
IA_SEM = threading.Semaphore(1)   # LM Studio atiende UNA consulta a la vez: enviar 2 solo hacía que la 2.ª expirara

ESQUEMA = """
CREATE TABLE IF NOT EXISTS sensores (
id TEXT PRIMARY KEY,
latitud REAL NOT NULL,
longitud REAL NOT NULL,
fecha_registro TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audios (
id INTEGER PRIMARY KEY AUTOINCREMENT,
sensor_id TEXT NOT NULL REFERENCES sensores(id),
nombre_archivo TEXT NOT NULL,
ruta TEXT NOT NULL,
hash_sha256 TEXT NOT NULL UNIQUE,
fecha_subida TEXT NOT NULL,
t_subida REAL NOT NULL,
estado TEXT NOT NULL DEFAULT 'pendiente');

CREATE TABLE IF NOT EXISTS analisis (
id INTEGER PRIMARY KEY AUTOINCREMENT,
audio_id INTEGER NOT NULL REFERENCES audios(id),
categoria TEXT NOT NULL,
confianza REAL NOT NULL,
nivel_alerta TEXT NOT NULL,
justificacion TEXT,
accion TEXT,
fuente TEXT NOT NULL,
modelo TEXT,
duracion_audio_s REAL,
t_audio_s REAL,
t_ia_s REAL,
caracteristicas TEXT,
etiqueta_heuristica TEXT,
fecha TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS alertas (
id INTEGER PRIMARY KEY AUTOINCREMENT,
analisis_id INTEGER NOT NULL REFERENCES analisis(id),
sensor_id TEXT NOT NULL REFERENCES sensores(id),
latitud REAL NOT NULL,
longitud REAL NOT NULL,
nivel TEXT NOT NULL,
mensaje TEXT NOT NULL,
t_alerta_s REAL,
fecha TEXT NOT NULL,
atendida INTEGER NOT NULL DEFAULT 0,
revision TEXT,
sms_enviado INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS sensor_config (
sensor_id TEXT PRIMARY KEY REFERENCES sensores(id),
nombre TEXT,
token TEXT NOT NULL,
activo INTEGER NOT NULL DEFAULT 1,
ultimo_contacto REAL,
audios_recibidos INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS destinatarios (
id INTEGER PRIMARY KEY AUTOINCREMENT,
nombre TEXT NOT NULL,
telefono TEXT NOT NULL UNIQUE,
activo INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS emparejamientos (
codigo TEXT PRIMARY KEY,
sensor_id TEXT NOT NULL,
expira REAL NOT NULL);

CREATE TABLE IF NOT EXISTS sms_log (
id INTEGER PRIMARY KEY AUTOINCREMENT,
alerta_id INTEGER,
telefono TEXT,
mensaje TEXT,
proveedor TEXT,
estado TEXT,
detalle TEXT,
fecha TEXT NOT NULL);
"""

def _ahora():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _hora_ms():
    t = time.time()
    ms = int((t - int(t)) * 1000)
    return f"{time.strftime('%H:%M:%S', time.localtime(t))}.{ms:03d}"

@contextmanager
def conectar():
    with DB_LOCK:
        con = sqlite3.connect(ARCHIVO_BD, timeout=30.0, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

def inicializar():
    DIR_AUDIOS.mkdir(parents=True, exist_ok=True)
    with conectar() as bd:
        bd.executescript(ESQUEMA)
        columnas = [r["name"] for r in bd.execute("PRAGMA table_info(alertas)")]
        if "revision" not in columnas:
            bd.execute("ALTER TABLE alertas ADD COLUMN revision TEXT")
        if "sms_enviado" not in columnas:
            bd.execute("ALTER TABLE alertas ADD COLUMN sms_enviado INTEGER NOT NULL DEFAULT 0")
        if "origen" not in columnas:
            bd.execute("ALTER TABLE alertas ADD COLUMN origen TEXT")         # acustica | ia
        if "omitidos_silencio" not in [r["name"] for r in bd.execute("PRAGMA table_info(sensor_config)")]:
            bd.execute("ALTER TABLE sensor_config ADD COLUMN omitidos_silencio INTEGER NOT NULL DEFAULT 0")
        col_an = [r["name"] for r in bd.execute("PRAGMA table_info(analisis)")]
        if "ia_estado" not in col_an:
            bd.execute("ALTER TABLE analisis ADD COLUMN ia_estado TEXT")     # pendiente | hecho | fallo | omitida
        if "ia_intentos" not in col_an:
            bd.execute("ALTER TABLE analisis ADD COLUMN ia_intentos INTEGER NOT NULL DEFAULT 0")
        if "etiqueta" not in [r["name"] for r in bd.execute("PRAGMA table_info(audios)")]:
            bd.execute("ALTER TABLE audios ADD COLUMN etiqueta TEXT")        # categoría real marcada por una persona

def recuperar_interrumpidos():
    """Audios que quedaron 'procesando' por un cierre inesperado vuelven a la cola."""
    inicializar()
    with conectar() as bd:
        bd.execute("UPDATE audios SET estado='pendiente' WHERE estado='procesando'")
        hay_cola = bd.execute("SELECT 1 FROM analisis WHERE ia_estado='pendiente' LIMIT 1").fetchone()
    if hay_cola:
        COLA_IA.despertar()          # lo que quedó en la cola de IA al cerrar se retoma

def limpiar_todo():
    """Borra audios, análisis, alertas, historial de mensajes y la cola de IA. Conserva sensores (con su token),
    destinatarios, configuración y la carpeta 'entrenamiento'. Devuelve (archivos_borrados, archivos_no_borrados).

    Antes se borraba la carpeta entera de una vez: en Windows fallaba si cualquier audio estaba abierto (reproductor,
    antivirus, un audio llegando del celular), el error no se veía y los audios que quedaban se volvían a registrar.
    Ahora la base se vacía con SQL (siempre funciona) y los archivos se borran uno por uno; los que Windows no deja
    borrar se ignoran en adelante (no vuelven a aparecer en la tabla)."""
    inicializar()
    with DB_LOCK:
        with conectar() as bd:
            for t in ("alertas", "sms_log", "analisis", "audios", "emparejamientos"):
                bd.execute(f"DELETE FROM {t}")
            bd.execute("DELETE FROM sqlite_sequence WHERE name IN ('alertas','sms_log','analisis','audios')")
            bd.execute("UPDATE sensor_config SET audios_recibidos=0")
            bd.execute("CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT)")
            bd.execute("INSERT OR REPLACE INTO meta(clave, valor) VALUES('limpieza_t', ?)", (str(time.time()),))
        try:
            with conectar() as bd:
                bd.execute("VACUUM")                       # reduce el tamaño del archivo de la base
        except sqlite3.Error:
            pass
        _ULTIMO_SMS.clear()
    borrados, fallidos = 0, []
    if DIR_AUDIOS.exists():
        for ruta in sorted(DIR_AUDIOS.rglob("*"), key=lambda r: len(r.parts), reverse=True):
            try:
                if ruta.is_file():
                    ruta.unlink()
                    borrados += 1
                elif ruta.is_dir() and ruta != DIR_AUDIOS:
                    ruta.rmdir()
            except OSError:
                if ruta.is_file():
                    fallidos.append(ruta.name)
    return borrados, fallidos

def _momento_limpieza():
    try:
        with conectar() as bd:
            bd.execute("CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT)")
            f = bd.execute("SELECT valor FROM meta WHERE clave='limpieza_t'").fetchone()
        return float(f["valor"]) if f else 0.0
    except Exception:
        return 0.0

def reiniciar_analisis():
    with conectar() as bd:
        bd.execute("DELETE FROM alertas")
        bd.execute("DELETE FROM analisis")
        bd.execute("UPDATE audios SET estado='pendiente'")

def registrar_sensor(sensor_id):
    sensor_id = str(sensor_id).strip() or SENSOR_POR_DEFECTO
    lat, lon = SENSORES_CONOCIDOS.get(sensor_id, SENSORES_CONOCIDOS[SENSOR_POR_DEFECTO])
    with conectar() as bd:
        bd.execute("INSERT OR IGNORE INTO sensores(id,latitud,longitud,fecha_registro) VALUES(?,?,?,?)", (sensor_id, lat, lon, _ahora()))
        return bd.execute("SELECT * FROM sensores WHERE id=?", (sensor_id,)).fetchone()

def _sha256(ruta):
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()

def registrar_audio(ruta, sensor_id, estado="pendiente"):
    registrar_sensor(sensor_id)
    huella = _sha256(ruta)
    with conectar() as bd:
        if bd.execute("SELECT id FROM audios WHERE hash_sha256=?", (huella,)).fetchone():
            return None
        cur = bd.execute(
            "INSERT INTO audios(sensor_id,nombre_archivo,ruta,hash_sha256,fecha_subida,t_subida,estado) VALUES(?,?,?,?,?,?,?)",
            (sensor_id, ruta.name, str(ruta), huella, _ahora(), time.time(), estado)
        )
        return cur.lastrowid

def subir_audio(origen, sensor_id=SENSOR_POR_DEFECTO):
    inicializar()
    origen = Path(origen)
    if not origen.is_file():
        raise FileNotFoundError(f"No existe el archivo: {origen}")
    if origen.suffix.lower() not in EXTENSIONES_AUDIO:
        raise ValueError(f"Formato no soportado: {origen.suffix}")
    destino_dir = DIR_AUDIOS if sensor_id == SENSOR_POR_DEFECTO else DIR_AUDIOS / sensor_id
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / origen.name
    if destino.resolve() != origen.resolve():
        shutil.copy2(origen, destino)
    return registrar_audio(destino, sensor_id)

def escanear_entrada():
    inicializar()
    nuevos = 0
    limpieza = _momento_limpieza()
    for ruta in sorted(DIR_AUDIOS.rglob("*")):
        if not ruta.is_file() or ruta.suffix.lower() not in EXTENSIONES_AUDIO:
            continue
        try:
            mtime = ruta.stat().st_mtime
            if time.time() - mtime < 2.0:                  # todavía se está escribiendo: se lee en el próximo ciclo
                continue
            if mtime < limpieza:                           # quedó de antes de «Limpiar Todo» (Windows no dejó borrarlo)
                continue
        except OSError:
            continue
        sensor = SENSOR_POR_DEFECTO if ruta.parent == DIR_AUDIOS else ruta.parent.name
        if registrar_audio(ruta, sensor) is not None:
            nuevos += 1
    return nuevos

def audios_pendientes():
    with conectar() as bd:
        return bd.execute(
            "SELECT a.*,s.latitud,s.longitud FROM audios a JOIN sensores s ON s.id=a.sensor_id WHERE a.estado='pendiente' ORDER BY a.id"
        ).fetchall()

def marcar_audio(audio_id, estado):
    with conectar() as bd:
        bd.execute("UPDATE audios SET estado=? WHERE id=?", (estado, audio_id))

def guardar_analisis(audio_id, r):
    with conectar() as bd:
        cur = bd.execute(
            "INSERT INTO analisis(audio_id,categoria,confianza,nivel_alerta,justificacion,accion,fuente,modelo,duracion_audio_s,t_audio_s,t_ia_s,caracteristicas,etiqueta_heuristica,fecha) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                audio_id, r["categoria"], r["confianza"], r["nivel_alerta"],
                r.get("justificacion"), r.get("accion"), r["fuente"], r.get("modelo"),
                r.get("duracion_audio_s"), r.get("t_audio_s"), r.get("t_ia_s"),
                r.get("caracteristicas"), r.get("etiqueta_heuristica"), _ahora()
            )
        )
        return cur.lastrowid

def actualizar_analisis(analisis_id, r):
    with conectar() as bd:
        bd.execute(
            "UPDATE analisis SET categoria=?,confianza=?,nivel_alerta=?,justificacion=?,accion=?,fuente=?,modelo=?,t_ia_s=? WHERE id=?",
            (r["categoria"], r["confianza"], r["nivel_alerta"], r.get("justificacion"), r.get("accion"),
             r["fuente"], r.get("modelo"), r.get("t_ia_s"), analisis_id)
        )
def guardar_alerta(analisis_id, sensor_id, lat, lon, nivel, mensaje, t_alerta_s):
    with conectar() as bd:
        cur = bd.execute(
            "INSERT INTO alertas(analisis_id,sensor_id,latitud,longitud,nivel,mensaje,t_alerta_s,fecha) VALUES(?,?,?,?,?,?,?,?)",
            (analisis_id, sensor_id, lat, lon, nivel, mensaje, t_alerta_s, _ahora())
        )
        return cur.lastrowid

def alertas_activas():
    with conectar() as bd:
        return bd.execute("SELECT * FROM alertas WHERE atendida=0 ORDER BY id DESC").fetchall()

def resumen_resultados():
    with conectar() as bd:
        return bd.execute(
            """
            SELECT au.id audio_id, au.sensor_id, au.nombre_archivo, au.estado, au.etiqueta, au.ruta,
                   an.id analisis_id, an.categoria, an.confianza, an.nivel_alerta,
                   an.fuente, an.etiqueta_heuristica, an.t_audio_s, an.t_ia_s,
                   an.justificacion, an.ia_estado, al.t_alerta_s
            FROM audios au
            LEFT JOIN analisis an ON an.audio_id = au.id
            LEFT JOIN alertas al ON al.analisis_id = an.id
            ORDER BY au.id DESC
            """
        ).fetchall()

def contar(tabla):
    if tabla not in {"sensores", "audios", "analisis", "alertas"}:
        raise ValueError("Tabla no permitida")
    with conectar() as bd:
        return bd.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]

def _leer_con_ffmpeg(ruta, sr):
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-t", str(DURACION_MAX_S),
        "-i", str(ruta), "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"
    ]
    p = subprocess.run(cmd, capture_output=True, timeout=90)
    if p.returncode != 0 or not p.stdout:
        detalle = (p.stderr.decode(errors="ignore").strip().splitlines() or ["sin detalle"])[-1]
        raise ValueError(f"ffmpeg no pudo leer el audio: {detalle[:200]}")
    return np.frombuffer(p.stdout, dtype=np.float32).copy()

def _leer_wav(ruta, sr):
    sr0, datos = wavfile.read(ruta)
    if datos.ndim > 1:
        datos = datos.astype(np.float64).mean(axis=1)
    if datos.dtype == np.uint8:
        datos = (datos.astype(np.float32) - 128.0) / 128.0
    elif np.issubdtype(datos.dtype, np.integer):
        datos = datos.astype(np.float32) / float(np.iinfo(datos.dtype).max + 1)
    else:
        datos = datos.astype(np.float32)
    datos = np.nan_to_num(datos)
    if sr0 != sr:
        g = np.gcd(int(sr0), int(sr))
        datos = signal.resample_poly(datos, sr // g, int(sr0) // g).astype(np.float32)
    return datos[:int(sr * DURACION_MAX_S)]

def cargar_audio(ruta, sr=FRECUENCIA_MUESTREO):
    ruta = Path(ruta)
    if ruta.suffix.lower() == ".wav":
        try:
            x = _leer_wav(ruta, sr)
        except Exception:
            if shutil.which("ffmpeg"):
                x = _leer_con_ffmpeg(ruta, sr)
            else:
                raise
    elif shutil.which("ffmpeg"):
        x = _leer_con_ffmpeg(ruta, sr)
    else:
        raise RuntimeError("Para este formato hace falta tener instalado ffmpeg.")
    x = np.nan_to_num(x.astype(np.float32))
    if x.size < int(sr * 0.1):
        raise ValueError("El audio es demasiado corto.")
    x -= np.mean(x)
    peak = np.max(np.abs(x))
    if peak > 1.0:
        x /= peak
    return x

def _db(x):
    return 20.0 * np.log10(np.maximum(x, EPS))

def _segmentos(x, sr):
    n = int(SEGMENTO_S * sr)
    paso = int(PASO_S * sr)
    if len(x) <= n:
        yield 0.0, x
        return
    posiciones = list(range(0, len(x) - n + 1, paso))
    if posiciones[-1] != len(x) - n:
        posiciones.append(len(x) - n)
    for i in posiciones:
        yield i / sr, x[i:i + n]

@lru_cache(maxsize=4)
def _filtro_periodicidad(sr):
    # 70-3500 Hz: quita el retumbe del viento y el siseo agudo de la lluvia, que tapaban la periodicidad del motor.
    return signal.butter(4, [70.0, min(3500.0, sr / 2 - 200.0)], btype="bandpass", fs=sr, output="sos")

def _periodicidad(x, sr):
    """Periodicidad (0-1) y frecuencia fundamental. Antes se medía solo en los primeros 0,6 s del tramo; ahora se
    promedia la autocorrelación de varias ventanas de 0,4 s (las más sonoras), lo que la hace mucho más estable con ruido."""
    if len(x) < int(0.15 * sr):
        return 0.0, 0.0
    xf = signal.sosfiltfilt(_filtro_periodicidad(sr), x.astype(np.float64)) if len(x) > 3 * 27 else x.astype(np.float64)
    n = min(len(xf), int(0.4 * sr))
    inicios = range(0, len(xf) - n + 1, max(1, n // 2))
    tramos = [xf[i:i + n] for i in inicios]
    if len(tramos) > 6:   # solo las 6 ventanas más sonoras: los silencios entre sonidos no deben diluir la medida
        energia = [float(np.dot(t, t)) for t in tramos]
        tramos = [tramos[i] for i in np.argsort(energia)[-6:]]
    ventana = signal.windows.hann(n, sym=False)
    ac = np.zeros(n)
    for tramo in tramos:
        tramo = (tramo - tramo.mean()) * ventana
        a = signal.fftconvolve(tramo, tramo[::-1], mode="full")[n - 1:]
        if a[0] > EPS:
            ac += a / a[0]
    if ac[0] <= EPS:
        return 0.0, 0.0
    ac /= ac[0]
    lag_min = max(1, int(sr / 500))
    lag_max = min(n - 2, int(sr / 55))
    if lag_max <= lag_min:
        return 0.0, 0.0
    zona = ac[lag_min:lag_max + 1]
    peaks, _ = signal.find_peaks(zona, distance=max(1, int(sr / 500)))
    idx = peaks[np.argmax(zona[peaks])] if len(peaks) else int(np.argmax(zona))
    lag = lag_min + idx
    return float(sr / lag), float(np.clip(ac[lag], 0, 1))

def _caracteristicas_segmento(x, sr):
    if len(x) < int(0.2 * sr):
        return {}

    frame = min(int(FRAME_S * sr), len(x))
    hop = max(1, int(HOP_S * sr))
    frames = np.lib.stride_tricks.sliding_window_view(x, frame)[::hop]
    if len(frames) < 2:
        return {}

    ventana = signal.windows.hann(frame, sym=False)
    espectro = np.abs(np.fft.rfft(frames * ventana, axis=1)) + EPS
    potencia = espectro ** 2
    frecuencia = np.fft.rfftfreq(frame, 1.0 / sr)
    total = potencia.sum(axis=1) + EPS

    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    pico = np.max(np.abs(frames), axis=1)
    db = _db(rms)
    peak_db = _db(pico)

    # Los cuadros de silencio digital (relleno con ceros, cortes de transmisión del sensor) no son sonido del bosque:
    # se reemplazan por el piso del resto para que no deformen mediana y percentiles.
    sonoro = db > -100.0
    if sonoro.sum() >= 2 and not sonoro.all():
        db = np.where(sonoro, db, float(np.min(db[sonoro])))
        peak_db = np.where(sonoro, peak_db, float(np.min(db[sonoro])))

    onset = np.diff(db, prepend=db[0])
    mediana = float(np.median(db))
    p95 = float(np.percentile(db, 95))
    p10 = float(np.percentile(db, 10))
    # Actividad = fracción del tiempo en que la fuente principal suena (a menos de 10 dB de su nivel alto).
    # La versión anterior la medía contra la mediana y daba ~0 en motosierras reales que suenan casi todo el tramo.
    umbral_activo = max(-55.0, p95 - 10.0)
    activa = db > umbral_activo
    transitorios = int(np.sum(db > mediana + 9.0))
    impulsos = _impulsos(db, p10, len(x) / sr)

    i = int(np.argmax(onset))
    a = max(0, i - 12)
    b = min(len(db), i + 13)
    shock_db = float(max(0.0, db[i] - np.median(db[a:b])))
    peak_median = float(max(peak_db.max() - mediana, 0.0))

    def banda(a0, b0):
        m = (frecuencia >= a0) & (frecuencia < b0)
        return float(potencia[:, m].sum() / (potencia.sum() + EPS))

    centroides = (frecuencia[None, :] * potencia).sum(axis=1) / total
    flatness = np.exp(np.mean(np.log(potencia), axis=1)) / (np.mean(potencia, axis=1) + EPS)
    zcr = float(np.mean(np.abs(np.diff(np.signbit(frames), axis=1))))
    f0, periodic = _periodicidad(x, sr)
    flux = np.sqrt(np.mean(np.diff(espectro / total[:, None], axis=0) ** 2, axis=1))
    decay = None

    if i < len(db) - 1:
        d = np.where(db[i:] < db[i] - 12.0)[0]
        if len(d):
            decay = float(d[0] * HOP_S * 1000.0)

    return {
        "rms_db": float(_db(np.sqrt(np.mean(x * x)))),
        "peak_db": float(_db(np.max(np.abs(x)))),
        "crest_db": float(np.median(peak_db - db)),
        "active": float(np.mean(activa)),
        "range_db": float(p95 - p10),
        "transients": transitorios,
        "shock_db": shock_db,
        "peak_median_db": peak_median,
        "broadband": banda(1000, 8000),
        "low": banda(0, 500),
        "mid": banda(1000, 3000),
        "high": banda(3000, 8000),
        "centroid": float(np.mean(centroides)),
        "flatness": float(np.mean(flatness)),
        "zcr": zcr,
        "f0": float(f0),
        "periodic": float(periodic),
        "decay_ms": decay,
        "flux": float(np.median(flux)),
        **impulsos
    }

def _impulsos(db, piso, dur_s):
    """Cuenta golpes acústicos separados (subida >= 9 dB en 30 ms, a 12 dB sobre el piso, separados >= 80 ms).
    Un disparo da 1-3 golpes que dominan el tramo; fuego, gotas, lluvia o pisadas dan muchos golpes parecidos."""
    if len(db) < 5:
        return {"n_impulsos": 0, "tasa_impulsos": 0.0, "dominancia_db": 0.0}
    subida = db - np.minimum.reduce([np.roll(db, k) for k in (1, 2, 3)])
    subida[:3] = 0.0
    cand = (subida >= 9.0) & (db >= piso + 12.0)
    altura = np.where(cand, db, -300.0)
    picos, _ = signal.find_peaks(np.r_[-300.0, altura, -300.0], height=-299.0, distance=8)
    picos = picos - 1
    niveles = np.sort(db[picos])[::-1] if len(picos) else np.array([])
    if len(niveles) >= 2:
        dominancia = float(niveles[0] - np.median(niveles[1:]))
    elif len(niveles) == 1:
        dominancia = float(niveles[0] - piso)
    else:
        dominancia = 0.0
    return {"n_impulsos": int(len(picos)), "tasa_impulsos": float(len(picos) / max(dur_s, 0.5)),
            "dominancia_db": dominancia}

def _prom(seg, k):
    v = [s[k] for s in seg if k in s and s[k] is not None]
    return float(np.mean(v)) if v else 0.0

def _max(seg, k):
    v = [s[k] for s in seg if k in s and s[k] is not None]
    return float(max(v)) if v else 0.0

def _redondear(v):
    if isinstance(v, dict):
        return {k: _redondear(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_redondear(x) for x in v]
    if isinstance(v, (float, np.floating)):
        return round(float(v), 4)
    if isinstance(v, (int, np.integer)):
        return int(v)
    return v

def extraer_caracteristicas(x, sr=FRECUENCIA_MUESTREO):
    segmentos = []
    for inicio, seg in _segmentos(x, sr):
        c = _caracteristicas_segmento(seg, sr)
        if c:
            c["inicio_s"] = round(inicio, 2)
            segmentos.append(c)

    if not segmentos:
        raise ValueError("No se pudieron extraer características.")

    g = _caracteristicas_segmento(x, sr)
    g["duracion_s"] = round(len(x) / sr, 3)
    g["segmentos"] = segmentos
    g["active_avg"] = _prom(segmentos, "active")
    g["active_max"] = _max(segmentos, "active")
    g["periodic_avg"] = _prom(segmentos, "periodic")
    g["periodic_max"] = _max(segmentos, "periodic")
    g["f0_avg"] = _prom(segmentos, "f0")
    g["shock_max"] = _max(segmentos, "shock_db")
    g["peak_median_max"] = _max(segmentos, "peak_median_db")
    g["broadband_max"] = _max(segmentos, "broadband")
    g["low_avg"] = _prom(segmentos, "low")
    g["mid_avg"] = _prom(segmentos, "mid")
    g["high_avg"] = _prom(segmentos, "high")
    g["flatness_avg"] = _prom(segmentos, "flatness")
    g["centroid_avg"] = _prom(segmentos, "centroid")
    g["flux_avg"] = _prom(segmentos, "flux")
    g["crest_max"] = _max(segmentos, "crest_db")
    g["transient_max"] = int(_max(segmentos, "transients"))
    g["rms_max_seg"] = _max(segmentos, "rms_db") if segmentos else g.get("rms_db", -120.0)
    g["impulsos_max_seg"] = int(_max(segmentos, "n_impulsos"))
    return _redondear(g)

def _clip(x):
    return float(np.clip(x, 0.0, 1.0))

def _val(c, k_global, k_seg, defecto=0.0):
    if k_global in c and c[k_global] is not None:
        return c[k_global]
    if k_seg in c and c[k_seg] is not None:
        return c[k_seg]
    return defecto

def _shot_score(c):
    shock_v = _val(c, "shock_max", "shock_db", 0.0)
    peak_v = _val(c, "peak_median_max", "peak_median_db", 0.0)
    broad_v = _val(c, "broadband_max", "broadband", 0.0)
    active_v = _val(c, "active_max", "active", 0.0)
    per_v = _val(c, "periodic_max", "periodic", 0.0)
    t = _val(c, "transient_max", "transients", 0)

    shock = _clip((shock_v - 7.0) / 14.0)
    peak = _clip((peak_v - 7.0) / 16.0)
    broadband = _clip((broad_v - 0.14) / 0.50)
    active = _clip((0.55 - active_v) / 0.55)
    periodic = _clip((0.38 - per_v) / 0.38)
    trans = 1.0 if 1 <= t <= 12 else 0.45 if t <= 18 else 0.0
    return 0.30 * shock + 0.20 * peak + 0.16 * broadband + 0.12 * active + 0.09 * periodic + 0.13 * trans

def _chainsaw_score(c):
    act_v = _val(c, "active_avg", "active", 0.0)
    per_v = _val(c, "periodic_max", "periodic", 0.0)
    f0 = _val(c, "f0_avg", "f0", 0.0)
    mid_v = _val(c, "mid_avg", "mid", 0.0)
    high_v = _val(c, "high_avg", "high", 0.0)
    low_v = _val(c, "low_avg", "low", 0.0)
    flat_v = _val(c, "flatness_avg", "flatness", 0.0)

    active = _clip((act_v - 0.42) / 0.42)
    periodic = _clip((per_v - 0.22) / 0.42)
    fund = float(np.exp(-((f0 - 180.0) / 150.0) ** 2)) if f0 > 0 else 0.0
    mid = _clip((mid_v - 0.07) / 0.25)
    high = _clip((high_v - 0.015) / 0.13)
    low_pen = _clip((low_v - 0.74) / 0.26)
    flat_pen = _clip((flat_v - 0.60) / 0.38)
    return max(0.0, 0.22 * active + 0.27 * periodic + 0.15 * fund + 0.17 * mid + 0.08 * high - 0.07 * low_pen - 0.04 * flat_pen)

def _vehicle_score(c):
    act_v = _val(c, "active_avg", "active", 0.0)
    low_v = _val(c, "low_avg", "low", 0.0)
    per_v = _val(c, "periodic_avg", "periodic", 0.0)
    mid_v = _val(c, "mid_avg", "mid", 0.0)
    flat_v = _val(c, "flatness_avg", "flatness", 0.0)
    t_v = _val(c, "transient_max", "transients", 0)
    f0 = _val(c, "f0_avg", "f0", 0.0)

    active = _clip((act_v - 0.55) / 0.32)
    low = _clip((low_v - 0.52) / 0.35)
    periodic = _clip((per_v - 0.20) / 0.30)
    mid = _clip((0.28 - mid_v) / 0.28)
    flat = _clip((0.58 - flat_v) / 0.58)
    trans = _clip((14.0 - t_v) / 14.0)
    engine = float(np.exp(-((f0 - 105.0) / 95.0) ** 2)) if f0 > 0 else 0.35
    score = 0.25 * active + 0.29 * low + 0.19 * periodic + 0.09 * mid + 0.08 * flat + 0.05 * trans + 0.05 * engine
    if flat_v >= 0.64:
        score *= 0.35
    if per_v < 0.15:
        score *= 0.40
    if act_v < 0.48:
        score *= 0.45
    return float(np.clip(score, 0.0, 1.0))

def _ambient_score(c, max_mecanico):
    flat_v = _val(c, "flatness_avg", "flatness", 0.0)
    flux_v = _val(c, "flux_avg", "flux", 0.0)
    per_v = _val(c, "periodic_avg", "periodic", 0.0)
    mid_v = _val(c, "mid_avg", "mid", 0.0)

    flat = _clip(flat_v / 0.72)
    flux = _clip(flux_v / 0.08)
    no_periodic = _clip((0.30 - per_v) / 0.30)
    no_mec = _clip(1.0 - max_mecanico)
    mid = _clip((0.55 - mid_v) / 0.55)
    score = 0.24 * flat + 0.20 * flux + 0.21 * no_periodic + 0.20 * no_mec + 0.15 * mid
    if flat > 0.52 and no_periodic > 0.45:
        score += 0.15
    return float(np.clip(score, 0.0, 1.0))

def _other_score(c, max_mecanico):
    act_v = _val(c, "active_avg", "active", 0.0)
    flat_v = _val(c, "flatness_avg", "flatness", 0.0)
    return float(np.clip(0.30 + 0.20 * abs(0.5 - act_v) + 0.20 * flat_v - 0.50 * max_mecanico, 0.0, 1.0))

def _pocos_golpes(s):
    """Un disparo (o una ráfaga corta de caza) son 1-3 golpes que dominan; fuego, gotas, pisadas o golpes de
    madera producen golpes seguidos y parecidos. Era la principal fuente de falsas alarmas de 'disparo'."""
    n = s.get("n_impulsos", 0)
    return n <= 3 or (n <= 5 and s.get("dominancia_db", 0) >= 12.0)

def _es_hard_shot(c, shot_score):
    segs = c.get("segmentos", [])
    ritmo_ok = c.get("tasa_impulsos", 0.0) <= 1.0 or any(
        s.get("n_impulsos", 0) <= 2 and s.get("dominancia_db", 0) >= 12.0 for s in segs)
    seg_impulso = any(
        s.get("shock_db", 0) >= 11.5 and s.get("peak_median_db", 0) >= 9.5 and s.get("broadband", 0) >= 0.14 and s.get("periodic", 0) < 0.42
        and _pocos_golpes(s)
        for s in segs
    )
    global_impulso = (
        c.get("shock_max", 0) >= 12.0 and c.get("peak_median_max", 0) >= 9.5 and c.get("broadband_max", 0) >= 0.15 and c.get("periodic_max", 0) < 0.42
        and _pocos_golpes(c)
    )
    return (seg_impulso or global_impulso) and ritmo_ok and shot_score >= 0.52

def _es_hard_chain(c, chain_score):
    segs = c.get("segmentos", [])
    seg_sierra = any(
        s.get("active", 0) >= 0.48 and s.get("periodic", 0) >= 0.25 and 55 <= s.get("f0", 0) <= 500 and s.get("mid", 0) >= 0.07 and s.get("flatness", 1) < 0.58
        for s in segs
    )
    global_sierra = (
        chain_score >= 0.60 and c.get("active_avg", 0) >= 0.45 and c.get("periodic_max", 0) >= 0.25 and 55 <= c.get("f0_avg", 0) <= 500 and c.get("mid_avg", 0) >= 0.07
    )
    return (seg_sierra or global_sierra) and chain_score >= 0.56

def _es_hard_vehicle(c, vehicle_score):
    return (
        vehicle_score >= 0.64 and c.get("active_avg", 0) >= 0.55 and c.get("low_avg", 0) >= 0.52 and c.get("periodic_avg", 0) >= 0.20 and c.get("flatness_avg", 0) < 0.58 and c.get("mid_avg", 0) < 0.28
    )

def clasificar_por_reglas(c):
    if c.get("rms_max_seg", c.get("rms_db", -120)) < -62:
        return "ambiente_natural", 0.95, {"disparo": 0.0, "motosierra": 0.0, "motor_vehiculo": 0.0, "ambiente_natural": 0.96, "otro": 0.04}

    segs = c.get("segmentos", [])
    shot = max(_shot_score(c), max((_shot_score(s) for s in segs), default=0.0))
    chain = max(_chainsaw_score(c), max((_chainsaw_score(s) for s in segs), default=0.0))
    vehicle = max(_vehicle_score(c), max((_vehicle_score(s) for s in segs), default=0.0))
    max_mec = max(shot, chain, vehicle)
    ambient = _ambient_score(c, max_mec)
    other = _other_score(c, max_mec)

    scores = {"disparo": shot, "motosierra": chain, "motor_vehiculo": vehicle, "ambiente_natural": ambient, "otro": other}

    hard_shot = _es_hard_shot(c, shot)
    hard_chain = _es_hard_chain(c, chain)
    hard_vehicle = _es_hard_vehicle(c, vehicle)

    if hard_shot and (not hard_chain or shot >= chain):
        categoria = "disparo"
    elif hard_chain:
        categoria = "motosierra"
    elif hard_vehicle:
        categoria = "motor_vehiculo"
    elif ambient >= 0.46 and ambient >= max_mec - 0.05:
        categoria = "ambiente_natural"
    else:
        categoria = max(scores, key=scores.get)

    if categoria == "disparo" and not hard_shot:
        categoria = "ambiente_natural" if ambient >= 0.45 else "otro"

    if categoria == "motosierra" and not (hard_chain or (chain >= 0.58 and c.get("active_max", 0) >= 0.45 and c.get("periodic_max", 0) >= 0.24)):
        categoria = "ambiente_natural" if ambient >= 0.45 else "otro"

    if categoria == "motor_vehiculo" and not hard_vehicle:
        categoria = "ambiente_natural"

    valores = sorted(scores.values(), reverse=True)
    confianza = 0.38 + 0.46 * valores[0] + 0.16 * _clip((valores[0] - valores[1]) / 0.45)

    if categoria == "disparo" and hard_shot:
        confianza = max(confianza, 0.85)
    elif categoria == "motosierra" and hard_chain:
        confianza = max(confianza, 0.85)
    elif categoria == "motor_vehiculo" and hard_vehicle:
        confianza = max(confianza, 0.78)
    elif categoria == "ambiente_natural" and (c.get("flatness_avg", 0) >= 0.50 or c.get("periodic_avg", 0) < 0.20):
        confianza = max(confianza, 0.78)

    return categoria, round(float(np.clip(confianza, 0.0, 0.98)), 3), {k: round(float(v), 3) for k, v in scores.items()}

# ======================================================================================
# RED NEURONAL (YAMNet) + FUSIÓN CON LAS REGLAS
# ======================================================================================
_RED = None
_RED_ERROR = None
_RED_LOCK = threading.Lock()

def red_neuronal():
    """Devuelve el clasificador YAMNet (se carga una sola vez, ~0,3 s) o None si no está disponible."""
    global _RED, _RED_ERROR
    if not RED_ACTIVA or ClasificadorSonidos is None:
        return None
    if _RED is None and _RED_ERROR is None:
        with _RED_LOCK:
            if _RED is None and _RED_ERROR is None:
                try:
                    _RED = ClasificadorSonidos.compartido()
                except Exception as e:
                    _RED_ERROR = str(e)
    return _RED

def estado_red():
    if not RED_ACTIVA:
        return "desactivada (RED_NEURONAL=0)"
    if ClasificadorSonidos is None:
        return "no disponible (falta clasificador_sonidos.py)"
    red = red_neuronal()
    return "YAMNet lista (521 clases de AudioSet)" if red else f"no disponible: {_RED_ERROR}"

def _ganancia_automatica(x, sr):
    """Sube el volumen de grabaciones débiles (fuente lejana) hasta RED_NIVEL_OBJETIVO_DB, como máximo
    RED_GANANCIA_MAX_DB. La red fue entrenada con audio a volumen normal: una motosierra a -55 dB pasó de
    detectarse 2/12 a 9/12 veces en las pruebas. Nunca baja el volumen ni satura."""
    n = int(sr)
    if len(x) <= n:
        mas_alto = float(np.sqrt(np.mean(x * x)) + EPS)
    else:
        mas_alto = max(float(np.sqrt(np.mean(x[i:i + n] ** 2)) + EPS) for i in range(0, len(x) - n + 1, n // 2))
    g_db = float(np.clip(RED_NIVEL_OBJETIVO_DB - _db(mas_alto), 0.0, RED_GANANCIA_MAX_DB))
    if g_db < 0.5:
        return x, 0.0
    y = x * (10.0 ** (g_db / 20.0))
    pico = float(np.max(np.abs(y)))
    if pico > 0.99:
        y = y * (0.99 / pico)
    return y.astype(np.float32), round(g_db, 1)

def _reducir_ruido(x, sr):
    """Resta el ruido de fondo constante (soplido del micrófono del celular, zumbido) estimado en los momentos más
    silenciosos. Con grabaciones débiles, la ganancia automática subía ese soplido y la red oía 'White noise' en vez
    del disparo. Devuelve None si el sonido no tiene golpes (en una motosierra continua se borraría la propia sierra)."""
    n, h = 512, 256
    if len(x) < 4 * n:
        return None
    f, t, Z = signal.stft(x, fs=sr, nperseg=n, noverlap=n - h)
    mag = np.abs(Z)
    energia = 10 * np.log10((mag ** 2).sum(axis=0) + EPS)
    if np.percentile(energia, 98) - np.percentile(energia, 20) < 12.0:
        return None                                   # sonido estacionario: no hay "silencios" para medir el ruido
    tranquilos = energia <= np.percentile(energia, 20)
    ruido = np.median(mag[:, tranquilos], axis=1, keepdims=True)
    limpio = np.maximum(mag - 1.5 * ruido, 0.08 * mag)  # resta espectral con piso (evita sonido "metálico")
    _, y = signal.istft(limpio * np.exp(1j * np.angle(Z)), fs=sr, nperseg=n, noverlap=n - h)
    return y[:len(x)].astype(np.float32)

def escuchar_con_red(x, sr=FRECUENCIA_MUESTREO):
    """Resultado de la red: puntajes por categoría del proyecto, top-5 de AudioSet y línea de tiempo.
    Si el sonido tiene golpes, se escucha también una versión sin ruido de fondo y se queda la evidencia más fuerte
    de amenaza de las dos (lo natural se toma del audio original)."""
    red = red_neuronal()
    if red is None:
        return None
    y, g_db = _ganancia_automatica(x, sr)
    r = red.analizar(y, sr)
    r["ganancia_db"] = g_db
    limpio = _reducir_ruido(x, sr)
    if limpio is not None:
        y2, _ = _ganancia_automatica(limpio, sr)
        r2 = red.analizar(y2, sr)
        amenaza = lambda q: max(q["categorias"].get(k, 0.0) for k in CATEGORIAS_AMENAZA)
        if amenaza(r2) > amenaza(r):
            for k in CATEGORIAS_AMENAZA:
                r["categorias"][k] = max(r["categorias"].get(k, 0.0), r2["categorias"].get(k, 0.0))
            r["top"], r["huella"], r["sin_ruido"] = r2["top"], r2["huella"], True
    return r

_AJUSTE = None
_AJUSTE_CARGADO = False

def ajuste_local():
    """Modelo entrenado con las grabaciones propias (modelos/ajuste_local.npz) o None si no se ha entrenado."""
    global _AJUSTE, _AJUSTE_CARGADO
    if not _AJUSTE_CARGADO:
        _AJUSTE_CARGADO = True
        try:
            if AjusteLocal is not None and (RAIZ / "modelos" / "ajuste_local.npz").exists():
                _AJUSTE = AjusteLocal(RAIZ / "modelos" / "ajuste_local.npz")
        except Exception:
            _AJUSTE = None
    return _AJUSTE

def _aplicar_ajuste_local(c, cat, conf, motivo, rc):
    """El modelo entrenado con audios propios conoce los micrófonos y el bosque reales: cuando está muy seguro, decide.
    Excepción de seguridad: no anula una amenaza que la red neuronal oye con claridad."""
    ml = c.get("modelo_local")
    if not ml:
        return cat, conf, motivo
    probs = ml["probabilidades"]
    top = max(probs, key=probs.get)
    p = probs[top]
    umbral = 0.80 if ml["precision_cv"] >= 0.85 else 0.92
    if p < umbral:
        return cat, conf, motivo + f"; modelo propio: {top} {p:.0%} (no concluyente)"
    if top == cat:
        return cat, max(conf, min(0.97, 0.70 + 0.27 * p)), motivo + f"; el modelo entrenado con sus audios lo confirma ({p:.0%})"
    if top in CATEGORIAS_AMENAZA and rc.get(top, 0.0) < 0.10 and rc.get("ambiente_natural", 0.0) >= 0.40:
        # sonido que el modelo propio no conoce (p. ej. un animal que no estaba en el entrenamiento): la red oye
        # algo natural y nada de la amenaza, así que el modelo propio no puede crear la alerta por sí solo
        return cat, conf, motivo + f"; el modelo propio sugiere {top} {p:.0%}, pero la red oye un sonido natural: se descarta"
    if cat in CATEGORIAS_AMENAZA and top not in CATEGORIAS_AMENAZA and rc.get(cat, 0.0) >= 0.45:
        return cat, min(conf, 0.75), motivo + f"; el modelo propio dice {top} {p:.0%}, pero la red oye {cat} con claridad: se mantiene"
    return top, min(0.95, 0.65 + 0.30 * p), f"modelo entrenado con sus audios: {top} {p:.0%} (antes: {motivo})"

def clasificar_combinado(c, red=None):
    """Fusiona las reglas acústicas (rápidas, buenas con impulsos y señales limpias) con la red neuronal
    (buena con sonidos reales y con ruido de fondo). Devuelve (categoria, confianza, scores, explicacion)."""
    h, hc, scores = clasificar_por_reglas(c)
    c["reglas"] = {"categoria": h, "confianza": hc}       # se conserva aparte para la IA de texto
    if not red:
        return h, hc, scores, f"Reglas acústicas: {h} {hc:.0%} (red neuronal no disponible)."

    rc = red["categorias"]
    ym, yd, yf = rc.get("motosierra", 0.0), rc.get("disparo", 0.0), rc.get("incendio", 0.0)
    yv, ya = rc.get("motor_vehiculo", 0.0), rc.get("ambiente_natural", 0.0)
    trueno = rc.get("trueno_goteo", 0.0) >= 0.45 and yd < 0.6          # trueno o goteo, no disparo
    top_nombre, top_p = (red["top"][0] if red.get("top") else ("", 0.0))
    # la red reconoce con fuerza un sonido que NO es motosierra (campana, sirena, pito, aeronave, tren, voz, música)
    # o un sonido natural. No se usa "Vehicle"/"Engine": la red etiqueta así muchas motosierras reales.
    otro_sonido = rc.get("otros_sonidos", 0.0) >= 0.60 or ya >= 0.30
    segs = c.get("segmentos", [])
    shot = max(_shot_score(c), max((_shot_score(s) for s in segs), default=0.0))
    chain = max(_chainsaw_score(c), max((_chainsaw_score(s) for s in segs), default=0.0))
    hard_shot = _es_hard_shot(c, shot)
    hard_chain = _es_hard_chain(c, chain)
    impulsivo = c.get("shock_max", 0) >= 9.0 and c.get("peak_median_max", 0) >= 9.0
    pocos_golpes = c.get("tasa_impulsos", 0.0) <= 1.2 or any(_pocos_golpes(s) and s.get("shock_db", 0) >= 9 for s in segs)
    crepitar = c.get("tasa_impulsos", 0.0) >= 0.4 or c.get("impulsos_max_seg", 0) >= 2
    rapida = lambda v, a, b: a + (b - a) * _clip(v)        # escala lineal a, b

    # --- motosierra ---------------------------------------------------------------------------------------------
    if ym >= 0.45:
        cat, conf, motivo = "motosierra", rapida((ym - 0.45) / 0.5, 0.80, 0.97), "la red reconoce motosierra"
    elif ym >= 0.15 and (h == "motosierra" or hard_chain or chain >= 0.55):
        cat, conf, motivo = "motosierra", rapida((ym + chain) / 1.4, 0.74, 0.90), "reglas y red coinciden en motosierra"
    elif h == "motosierra" and ym < 0.10 and ya >= 0.40:
        cat, conf, motivo = "ambiente_natural", rapida(ya, 0.62, 0.85), "la red identifica sonido natural (insecto, ave...): se descarta motosierra"
    elif h == "motosierra" and otro_sonido:
        alt = "motor_vehiculo" if yv >= 0.30 else "ambiente_natural" if ya >= 0.30 else "otro"
        cat, conf, motivo = alt, 0.60, f"las reglas ven motosierra pero la red reconoce '{top_nombre}': se descarta"
    elif h == "motosierra":
        cat, conf, motivo = "motosierra", min(hc, 0.66), "solo las reglas ven motosierra (la red no reconoce el sonido: revisar)"
    # --- incendio (antes que disparo: el crepitar del fuego también son impulsos) ---------------------------------
    elif yf >= 0.25 and yf >= yd and yv < 0.4:
        cat = "incendio"
        conf = rapida((yf - 0.25) / 0.4, 0.64, 0.90) + (0.04 if crepitar else 0.0)
        motivo = "la red reconoce fuego" + (" y hay crepitar" if crepitar else "")
    # --- disparo -----------------------------------------------------------------------------------------------
    elif trueno and (hard_shot or h == "disparo" or yd >= 0.4):
        cat, conf, motivo = "ambiente_natural", 0.80, "la red identifica trueno o goteo: se descarta disparo"
    elif yd >= 0.45 and not (yf >= 0.25 and yf > yd):
        # la red oye disparos con claridad: basta, aunque el golpe llegue suavizado por eco o distancia, o sea una
        # ráfaga de varios tiros (antes se exigía un golpe aislado y se perdían)
        cat, conf, motivo = "disparo", rapida((yd - 0.45) / 0.45, 0.80, 0.96), "la red reconoce disparo(s)"
    elif hard_shot and yd < 0.08 and ya >= 0.90:
        cat, conf, motivo = "otro", 0.55, f"impulso, pero la red oye '{top_nombre}' y nada de disparo: revisar"
    elif hard_shot and (yd >= 0.15 or ya < 0.85 or c.get("shock_max", 0) >= 18):
        cat, conf, motivo = "disparo", rapida(max(yd, shot - 0.3) / 0.7, 0.82, 0.97), "impulso de disparo (pocos golpes, banda ancha)"
    elif yd >= 0.40 and impulsivo and pocos_golpes and shot >= 0.40:
        cat, conf, motivo = "disparo", rapida(yd, 0.72, 0.92), "la red reconoce disparo y hay un impulso aislado"
    elif h == "disparo":
        cat, conf, motivo = "otro", 0.55, "impulso dudoso: la red no lo reconoce como disparo"
    # --- otros -------------------------------------------------------------------------------------------------
    elif (yv >= 0.45 and yv >= ya - 0.15) or (h == "motor_vehiculo" and yv >= 0.20):
        cat, conf, motivo = "motor_vehiculo", rapida(max(yv, hc - 0.2), 0.62, 0.92), "motor de vehículo o aeronave"
    elif ya >= 0.25 or h == "ambiente_natural":
        cat, conf, motivo = "ambiente_natural", rapida(max(ya, hc - 0.1), 0.65, 0.95), "sonido natural del bosque"
    else:
        cat, conf, motivo = h, min(hc, 0.70), "sin coincidencia clara"

    cat, conf, motivo = _aplicar_ajuste_local(c, cat, conf, motivo, rc)
    top = ", ".join(f"{n} {p:.0%}" for n, p in red["top"][:3])
    scores = dict(scores, red_motosierra=ym, red_disparo=yd, red_incendio=yf, red_motor=yv, red_ambiente=ya)
    explicacion = f"{motivo}. Red: {top}. Reglas: {h} {hc:.0%}."
    return cat, round(float(np.clip(conf, 0.0, 0.98)), 3), scores, explicacion

def analisis_acustico(x, sr=FRECUENCIA_MUESTREO):
    """Etapa rápida completa (características + reglas + red). La usan el análisis normal y el banco de pruebas."""
    c = extraer_caracteristicas(x, sr)
    red = escuchar_con_red(x, sr)
    if red:
        c["red"] = {k: red[k] for k in ("categorias", "parches_con", "n_parches", "top", "ganancia_db")}
        aj = ajuste_local()
        if aj is not None and red.get("huella") is not None:
            c["modelo_local"] = {"probabilidades": {k: round(v, 3) for k, v in aj.probabilidades(red["huella"]).items()},
                                 "precision_cv": round(aj.precision_cv, 3)}
    cat, conf, scores, explicacion = clasificar_combinado(c, red)
    return c, cat, conf, scores, explicacion

# El texto del sistema NO cambia entre consultas: LM Studio reutiliza su caché (KV cache) y responde más rápido.
PROMPT_SISTEMA = """Eres el auditor de alertas de una red de micrófonos en un bosque de Colombia (Parque Chicaque, ODS 15).
Tú no escuchas el audio. Una red neuronal (YAMNet) SÍ lo escuchó y te dice qué oyó. Debes elegir UNA categoría:
motosierra, disparo, incendio, motor_vehiculo, ambiente_natural u otro.

QUÉ ES CADA CATEGORÍA (etiquetas de la red, en inglés):
- motosierra: Chainsaw, Power tool, Light engine, Sawing.
- disparo: Gunshot, gunfire, Explosion, Cap gun, Machine gun.
- incendio: Fire, Crackle.
- motor_vehiculo: Vehicle, Car, Truck, Motorcycle, Engine, Aircraft, Helicopter.
- ambiente_natural: Rain, Wind, Thunder, Water, Stream, Bird, Insect, Cricket, Frog, Animal, Wild animals, Silence.
- otro: sirenas, campanas, voces, música o nada claro.

CÓMO DECIDIR (en este orden):
0. Si aparece "Modelo entrenado con grabaciones de este bosque" con 80 % o más, es la evidencia más fiable: úsala.
1. Mira la línea "La red oyó". Si una categoría tiene 50 % o más, esa es la respuesta.
2. Las "reglas" son un detector simple que SE EQUIVOCA con frecuencia: confunde insectos, aves y gallos con
   motosierra, y gotas o pisadas con disparo. Si la red y las reglas no coinciden, CREE A LA RED.
3. Solo si la red no supera 50 % en ninguna categoría, usa las reglas y las métricas para decidir.
4. Insectos, aves, lluvia, viento, trueno y ranas son SIEMPRE ambiente_natural, aunque las reglas digan otra cosa.
5. El contexto (varias detecciones seguidas, sensores vecinos, disparo de noche) solo sube o baja la confianza.

EJEMPLOS:
Entrada: La red oyó: Rain 88 %, Water 70 %. Más probable según la red: ambiente_natural 90 %. Reglas: disparo 75 %.
Salida: {"justificacion": "La red oye lluvia; las reglas confunden las gotas con impulsos.", "categoria": "ambiente_natural", "confianza": 0.9, "accion": "Sin acción necesaria."}
Entrada: La red oyó: Chainsaw 71 %, Engine 60 %. Más probable según la red: motosierra 71 %. Reglas: otro 55 %.
Salida: {"justificacion": "La red reconoce motosierra con claridad.", "categoria": "motosierra", "confianza": 0.85, "accion": "Enviar patrulla al sensor: posible tala ilegal."}
Entrada: La red oyó: Bird 92 %, Chirp, tweet 80 %. Más probable según la red: ambiente_natural 92 %. Reglas: motosierra 80 %.
Salida: {"justificacion": "Son aves; las reglas confunden el canto con un motor.", "categoria": "ambiente_natural", "confianza": 0.9, "accion": "Sin acción necesaria."}

Responde SOLO con JSON, en este orden: justificacion (máx. 15 palabras, español), categoria, confianza (0 a 1),
accion (máx. 12 palabras)."""
ESQUEMA_JSON = {
    "type": "object",
    # El orden importa: con formato estricto el modelo escribe los campos en este orden. Si la categoría va primero,
    # un modelo pequeño la "adivina" sin razonar; escribiendo antes la justificación acierta mucho más.
    "properties": {
        "justificacion": {"type": "string", "maxLength": 200},
        "categoria": {"type": "string", "enum": CATEGORIAS},
        "confianza": {"type": "number", "minimum": 0, "maximum": 1},
        "accion": {"type": "string", "maxLength": 120}
    },
    "required": ["justificacion", "categoria", "confianza", "accion"],
    "additionalProperties": False
}
class ErrorLMStudio(Exception):
    pass

_IA_LOCK = threading.Lock()
_IA_LISTA = False
_IA_PAUSA_HASTA = 0.0
_FORMATO_IA = 0   # índice del response_format que el servidor aceptó la última vez

_HTTP_LOCAL = threading.local()

def _http():
    """Una sesión HTTP por hilo: reutiliza la conexión con LM Studio (keep-alive) en vez de abrir una nueva por consulta."""
    sesion = getattr(_HTTP_LOCAL, "sesion", None)
    if sesion is None:
        sesion = _HTTP_LOCAL.sesion = requests.Session()
    return sesion

def _base_servidor():
    """http://localhost:1234 (sin /v1)."""
    return re.sub(r"/v1/?$", "", LMSTUDIO_URL)

def listar_modelos():
    try:
        r = _http().get(f"{LMSTUDIO_URL}/models", timeout=3.5)
        r.raise_for_status()
        return [str(x["id"]) for x in r.json().get("data", []) if isinstance(x, dict) and x.get("id")]
    except Exception as e:
        raise ErrorLMStudio(f"No se pudo conectar con LM Studio: {e}") from e

def modelos_cargados():
    """Modelos de chat cargados en memoria (API nativa de LM Studio). None si no se puede saber."""
    try:
        r = _http().get(f"{_base_servidor()}/api/v1/models", timeout=4)
        r.raise_for_status()
        datos = r.json()
        lista = datos.get("models", datos.get("data", [])) if isinstance(datos, dict) else datos
        return [str(m.get("key") or m.get("id") or m.get("display_name"))
                for m in lista if m.get("type", "llm") == "llm" and m.get("loaded_instances")]
    except Exception:
        return None

def reiniciar_ia():
    """Olvida el modelo elegido, el formato de respuesta aprendido y la pausa por fallo ('Probar IA')."""
    global _IA_LISTA, _IA_PAUSA_HASTA, _FORMATO_IA
    _modelo_a_usar.cache_clear()
    _IA_LISTA = False
    _IA_PAUSA_HASTA = 0.0
    _FORMATO_IA = 0          # otro modelo puede aceptar un formato distinto: se vuelve a probar desde json_schema

@lru_cache(maxsize=1)
def _modelo_a_usar():
    if LMSTUDIO_MODELO:
        return LMSTUDIO_MODELO
    candidatos = [x for x in listar_modelos() if "embed" not in x.lower()]
    if not candidatos:
        raise ErrorLMStudio("No hay ningún modelo de chat disponible en LM Studio.")
    cargados = modelos_cargados() or []
    instruct = [m for m in candidatos if _parece_instruct(m)]
    orden = ([m for m in instruct if m in cargados] + [m for m in candidatos if m in cargados] + instruct + candidatos)
    return orden[0]

def _parece_instruct(nombre):
    n = nombre.lower()
    return any(k in n for k in ("instruct", "chat", "-it", "_it", "qwen", "mistral-7b-instruct", "ministral", "gemma",
                                "phi-3", "phi-4", "hermes", "granite", "nemotron", "llama-3.2", "llama-3.3"))

def cargar_modelo(modelo=None, contexto=None):
    """Pide a LM Studio cargar el modelo con un contexto pequeño (evita agotar la RAM y los timeouts)."""
    modelo = modelo or _modelo_a_usar()
    try:
        r = _http().post(f"{_base_servidor()}/api/v1/models/load",
                          json={"model": modelo, "context_length": int(contexto or LMSTUDIO_CONTEXTO)}, timeout=300)
    except Exception as e:
        raise ErrorLMStudio(f"No se pudo pedir la carga del modelo: {e}") from e
    if r.status_code >= 400:
        raise ErrorLMStudio(f"LM Studio rechazó la carga (HTTP {r.status_code}): {r.text[:200]}")
    return r.json()

def asegurar_modelo(callback_log=None):
    """Garantiza (una sola vez) que el modelo esté cargado con contexto pequeño. Evita la carga automática
    de LM Studio con contexto enorme, que llenaba la RAM y provocaba los errores de conexión."""
    global _IA_LISTA
    if _IA_LISTA:
        return
    with _IA_LOCK:
        if _IA_LISTA:
            return
        modelo = _modelo_a_usar()
        cargados = modelos_cargados()
        if cargados is not None and not any(modelo == c or modelo in c or c in modelo for c in cargados):
            if callback_log:
                callback_log(f"Cargando '{modelo}' en LM Studio con contexto {LMSTUDIO_CONTEXTO} (solo la primera vez)...")
            cargar_modelo(modelo, LMSTUDIO_CONTEXTO)
        _IA_LISTA = True

FORMATOS_RESPUESTA = [
    {"type": "json_schema", "json_schema": {"name": "auditoria_audio", "strict": True, "schema": ESQUEMA_JSON}},
    {"type": "json_object"},
    None,
]
NOMBRES_FORMATO = ["json_schema (estricto)", "json_object", "texto libre + extracción"]

def _consultar(modelo, mensajes, max_tokens, timeout_s, esquema=None):
    """Envía la consulta probando formatos de respuesta del más estricto al más libre y recuerda el que funcionó.
    'esquema' permite limitar las categorías que la IA puede elegir. Devuelve (texto, finish_reason)."""
    global _IA_PAUSA_HASTA, _FORMATO_IA
    ultimo_error = None
    for i in range(_FORMATO_IA, len(FORMATOS_RESPUESTA)):
        payload = {"model": modelo, "messages": mensajes, "temperature": 0.0, "top_p": 1.0,
                   "max_tokens": max_tokens, "stream": False}
        formato = FORMATOS_RESPUESTA[i]
        if formato and esquema and formato["type"] == "json_schema":
            formato = {"type": "json_schema", "json_schema": {"name": "auditoria_audio", "strict": True, "schema": esquema}}
        if formato:
            payload["response_format"] = formato
        try:
            r = _http().post(f"{LMSTUDIO_URL}/chat/completions", json=payload, timeout=(3.0, timeout_s))
        except requests.exceptions.ConnectionError as e:
            _IA_PAUSA_HASTA = time.time() + IA_PAUSA_TRAS_FALLO_S
            raise ErrorLMStudio("LM Studio no está disponible (¿servidor apagado? LM Studio > Developer > Start Server)") from e
        except requests.exceptions.Timeout as e:
            _IA_PAUSA_HASTA = time.time() + 10.0
            raise ErrorLMStudio(f"el modelo tardó más de {timeout_s:.0f} s (usa un modelo más pequeño o sube LMSTUDIO_TIMEOUT_S)") from e
        except Exception as e:
            raise ErrorLMStudio(str(e)) from e
        if r.status_code in (400, 404, 422) and i < len(FORMATOS_RESPUESTA) - 1:
            ultimo_error = f"HTTP {r.status_code}: {r.text[:120]}"
            continue                                             # el modelo no acepta ese formato: se prueba el siguiente
        try:
            r.raise_for_status()
            eleccion = r.json()["choices"][0]
            mensaje = eleccion.get("message") or {}
            texto = mensaje.get("content") or ""
            if not texto.strip() and mensaje.get("reasoning_content"):
                texto = mensaje["reasoning_content"]             # modelos de razonamiento que dejan la respuesta ahí
        except Exception as e:
            raise ErrorLMStudio(f"Respuesta inválida de LM Studio: {e}") from e
        _FORMATO_IA = i
        return texto, eleccion.get("finish_reason")
    raise ErrorLMStudio(ultimo_error or "sin respuesta")

def diagnosticar_ia():
    """Estado de la IA con el motivo si algo falla. Hace una consulta REAL de auditoría (mismo prompt y formato
    que en operación), así 'Probar IA' confirma que el modelo devuelve el JSON esperado. Siempre devuelve un dict."""
    estado = {"url": LMSTUDIO_URL, "servidor": False, "modelo": None, "cargados": None, "responde": False,
              "t_respuesta_s": None, "formato": None, "mensaje": "", "red_neuronal": estado_red()}
    reiniciar_ia()
    try:
        listar_modelos()
        estado["servidor"] = True
        estado["modelo"] = _modelo_a_usar()
    except ErrorLMStudio as e:
        estado["mensaje"] = (f"No hay conexión con LM Studio en {LMSTUDIO_URL}. Abre LM Studio > Developer > "
                             f"Local Server y pulsa Start Server. ({e})")
        return estado
    estado["cargados"] = modelos_cargados()
    t0 = time.perf_counter()
    def caso(red, etiquetas, reglas, decision, hora):
        return {"red": {"puntajes": red, "etiquetas": etiquetas, "trueno_o_goteo": 0.0, "sirena_campana_aeronave_voz": 0.0},
                "reglas": reglas, "decision_acustica": decision,
                "contexto": {"hora_local": hora, "detecciones_ultimos_30min_sensor": {}, "alertas_vecinos_15min": []}}
    casos = [
        ("motosierra", caso({"motosierra": 0.82, "disparo": 0.0, "incendio": 0.01, "motor_vehiculo": 0.4, "ambiente_natural": 0.05},
                            ["Chainsaw 82%", "Light engine (high frequency) 75%", "Engine 40%"],
                            {"categoria": "motosierra", "confianza": 0.85}, {"categoria": "motosierra", "confianza": 0.9}, "10:15")),
        ("ambiente_natural", caso({"motosierra": 0.02, "disparo": 0.0, "incendio": 0.0, "motor_vehiculo": 0.03, "ambiente_natural": 0.95},
                                  ["Insect 95%", "Cricket 80%", "Wild animals 70%"],
                                  {"categoria": "motosierra", "confianza": 0.70}, {"categoria": "otro", "confianza": 0.6}, "14:40")),
        ("disparo", caso({"motosierra": 0.0, "disparo": 0.78, "incendio": 0.02, "motor_vehiculo": 0.05, "ambiente_natural": 0.20},
                         ["Gunshot, gunfire 78%", "Explosion 45%", "Silence 30%"],
                         {"categoria": "disparo", "confianza": 0.88}, {"categoria": "disparo", "confianza": 0.9}, "02:10")),
    ]
    try:
        asegurar_modelo()
        aciertos, detalle = 0, []
        for esperado, prueba in casos:
            texto, fin = _consultar(estado["modelo"], [{"role": "system", "content": PROMPT_SISTEMA},
                                                       {"role": "user", "content": mensaje_para_ia(prueba)}],
                                    200, 60.0)
            d = _normalizar(_extraer_json(texto))
            aciertos += d["categoria"] == esperado
            detalle.append(f"{'✓' if d['categoria'] == esperado else '✗'} {esperado} -> {d['categoria']} {d['confianza']:.0%}")
        estado["responde"] = True
        estado["formato"] = NOMBRES_FORMATO[_FORMATO_IA]
        estado["t_respuesta_s"] = round((time.perf_counter() - t0) / len(casos), 2)
        estado["aciertos"] = aciertos
        msg = f"La IA responde con JSON válido ({estado['formato']}). Casos de prueba: {aciertos}/{len(casos)} correctos\n" + "\n".join(detalle)
        if aciertos < len(casos):
            msg += ("\n\nEl modelo razona mal este tipo de tarea. "
                    + ("Parece un modelo BASE (sin 'Instruct' en el nombre): descarga la versión Instruct, "
                       "p. ej. 'Meta-Llama-3.1-8B-Instruct' o 'Qwen2.5-7B-Instruct'. "
                       if not _parece_instruct(estado["modelo"]) else "Prueba otro modelo, p. ej. 'Qwen2.5-7B-Instruct'. ")
                    + "Mientras tanto no hay riesgo: el sistema ignora respuestas de la IA que contradicen la evidencia acústica.")
        estado["mensaje"] = msg
    except ErrorLMStudio as e:
        estado["mensaje"] = f"El modelo respondió, pero no con el formato esperado: {e}" if estado["servidor"] else str(e)
    except requests.exceptions.Timeout:
        estado["mensaje"] = ("El modelo no respondió en 60 s. Causa típica: contexto demasiado grande o RAM llena. "
                             "En LM Studio descarga el modelo y vuelve a cargarlo con Context Length 4096.")
    except Exception as e:
        estado["mensaje"] = f"El servidor está activo pero la consulta falló: {e}"
    return estado

def _extraer_json(texto):
    texto = re.sub(r"<think>.*?(</think>|$)", "", str(texto or ""), flags=re.I | re.S)
    texto = re.sub(r"```(?:json)?", "", texto, flags=re.I).replace("```", "").strip()
    ini, fin = texto.find("{"), texto.rfind("}")
    if ini < 0 or fin <= ini:
        raise ErrorLMStudio("LM Studio no devolvió JSON.")
    try:
        return json.loads(texto[ini:fin + 1])
    except json.JSONDecodeError as e:
        raise ErrorLMStudio(f"JSON inválido de LM Studio: {e}") from e

def _normalizar(d):
    categoria = str(d.get("categoria", "otro")).strip().lower().replace(" ", "_")
    categoria = {"tala": "motosierra", "caza": "disparo", "fuego": "incendio", "vehiculo": "motor_vehiculo",
                 "ambiente": "ambiente_natural", "natural": "ambiente_natural"}.get(categoria, categoria)
    if categoria not in CATEGORIAS:
        categoria = "otro"
    try:
        confianza = float(d.get("confianza", 0))
        if confianza > 1.0:                               # algunos modelos responden 85 en vez de 0.85
            confianza /= 100.0
    except Exception:
        confianza = 0.0
    return {
        "categoria": categoria,
        "confianza": round(float(np.clip(confianza, 0.0, 1.0)), 3),
        "justificacion": str(d.get("justificacion", "")).strip()[:300],
        "accion": str(d.get("accion") or d.get("accion_recomendada") or "").strip()[:200]
    }

def _resumen_para_ia(c, etiqueta_h, confianza_h, contexto=None):
    """Lo que la IA necesita para decidir, en pocas líneas: lo que oyó la red, lo que dicen las reglas, 6 métricas
    con nombre claro y el contexto. Menos de la mitad de tokens que antes y con información que la IA sí puede usar."""
    red = c.get("red") or {}
    r = {
        "red": ({"puntajes": {k: round(v, 2) for k, v in red["categorias"].items() if k in CATEGORIAS},
                 "etiquetas": [f"{n} {p:.0%}" for n, p in red["top"][:5]],
                 "trueno_o_goteo": round(red["categorias"].get("trueno_goteo", 0.0), 2),
                 "sirena_campana_aeronave_voz": round(red["categorias"].get("otros_sonidos", 0.0), 2)}
                if red else "no disponible"),
        "reglas": c.get("reglas") or {"categoria": etiqueta_h, "confianza": round(confianza_h, 2)},
        **({"modelo_propio": dict(sorted(c["modelo_local"]["probabilidades"].items(), key=lambda kv: -kv[1])[:3])}
           if c.get("modelo_local") else {}),
        "decision_acustica": {"categoria": etiqueta_h, "confianza": round(confianza_h, 2)},
        "metricas": {
            "duracion_s": c.get("duracion_s"),
            "nivel_db": round(c.get("rms_max_seg", c.get("rms_db", 0)), 1),
            "golpes": c.get("n_impulsos", 0),
            "golpes_por_s": round(c.get("tasa_impulsos", 0.0), 2),
            "golpe_mas_fuerte_db": round(c.get("shock_max", 0.0), 1),
            "periodicidad": round(c.get("periodic_max", 0.0), 2),
            "f0_hz": round(c.get("f0_avg", 0.0)),
            "activo": round(c.get("active_avg", 0.0), 2),
        },
    }
    if contexto:
        r["contexto"] = contexto
    return r

NOMBRES_CAT = {"motosierra": "motosierra", "disparo": "disparo", "incendio": "incendio",
               "motor_vehiculo": "motor_vehiculo", "ambiente_natural": "ambiente_natural", "otro": "otro"}

def mensaje_para_ia(datos):
    """Convierte la evidencia en un texto corto y directo (lo que mejor entienden los modelos de 7-8B) y añade los
    datos completos en JSON al final. La primera línea repite el patrón de los ejemplos del prompt."""
    red = datos.get("red")
    lineas = []
    if isinstance(red, dict):
        p = red.get("puntajes", {})
        mejor = max(p, key=p.get) if p else "otro"
        lineas.append(f"La red oyó: {', '.join(red.get('etiquetas', [])[:4])}. "
                      f"Más probable según la red: {mejor} {p.get(mejor, 0):.0%}.")
    else:
        lineas.append("La red neuronal no está disponible: decide con las reglas y las métricas.")
    if datos.get("modelo_propio"):
        lineas.append("Modelo entrenado con grabaciones de este bosque: " +
                      ", ".join(f"{k} {v:.0%}" for k, v in datos["modelo_propio"].items()) + ".")
    reg = datos.get("reglas") or {}
    lineas.append(f"Reglas: {reg.get('categoria', 'otro')} {float(reg.get('confianza', 0)):.0%}.")
    ctx = datos.get("contexto") or {}
    if ctx:
        hist = next((v for k, v in ctx.items() if k.startswith("detecciones")), {}) or {}
        vec = ctx.get("alertas_vecinos_15min") or []
        lineas.append(f"Contexto: hora {ctx.get('hora_local', '?')}; detecciones recientes del sensor: "
                      f"{', '.join(f'{k} x{v}' for k, v in hist.items()) or 'ninguna'}; "
                      f"alertas de vecinos: {', '.join(vec) or 'ninguna'}.")
    return "\n".join(lineas) + "\nDatos completos: " + json.dumps(datos, ensure_ascii=False, separators=(",", ":"))

def contexto_para_ia(audio, minutos=30, radio_km=2.0):
    """Hora local, detecciones recientes del mismo sensor y alertas de sensores vecinos (lo que un humano revisaría)."""
    t = float(audio.get("t_subida") or time.time())
    ctx = {"hora_local": time.strftime("%H:%M", time.localtime(t))}
    try:
        with conectar() as bd:
            filas = bd.execute(
                "SELECT an.categoria, COUNT(*) n FROM analisis an JOIN audios au ON au.id=an.audio_id "
                "WHERE au.sensor_id=? AND au.t_subida>=? AND au.id<>? GROUP BY an.categoria",
                (audio["sensor_id"], t - minutos * 60, audio["id"])).fetchall()
            ctx[f"detecciones_ultimos_{minutos}min_sensor"] = {f["categoria"]: f["n"] for f in filas}
            vecinas = bd.execute(
                "SELECT al.sensor_id, al.latitud, al.longitud, an.categoria FROM alertas al "
                "JOIN analisis an ON an.id=al.analisis_id JOIN audios au ON au.id=an.audio_id "
                "WHERE al.sensor_id<>? AND au.t_subida>=?", (audio["sensor_id"], t - 15 * 60)).fetchall()
        lat, lon = audio.get("latitud"), audio.get("longitud")
        if lat is not None:
            ctx["alertas_vecinos_15min"] = sorted({
                f"{v['sensor_id']} {v['categoria']} a {distancia_km((lat, lon), (v['latitud'], v['longitud'])):.1f} km"
                for v in vecinas if distancia_km((lat, lon), (v["latitud"], v["longitud"])) <= radio_km})[:5]
    except Exception:
        pass
    return ctx

def candidatos_para_ia(c, etiqueta_h):
    """Categorías entre las que la IA puede elegir: las que la evidencia acústica hace posibles. La IA no escucha el
    audio; dejarla elegir entre las 6 categorías hacía que a veces escogiera una sin ningún respaldo."""
    red = (c.get("red") or {}).get("categorias") or {}
    cand = {etiqueta_h, "otro"}
    if (c.get("reglas") or {}).get("categoria"):
        cand.add(c["reglas"]["categoria"])
    cand |= {k for k, v in red.items() if k in CATEGORIAS and v >= 0.15}
    cand |= {k for k, v in ((c.get("modelo_local") or {}).get("probabilidades") or {}).items() if v >= 0.25}
    if red and red.get("ambiente_natural", 0) >= 0.10:
        cand.add("ambiente_natural")
    return [k for k in CATEGORIAS if k in cand]

def clasificar_con_ia(c, etiqueta_h, confianza_h, scores_h=None, callback_log=None, contexto=None):
    """Consulta a LM Studio como AUDITOR de la detección acústica. Robustez:
    - formato JSON estricto si el modelo lo admite (se recuerda cuál funcionó);
    - si la respuesta se corta (modelos que 'piensan' en voz alta) o no es JSON, UN reintento con más espacio;
    - si LM Studio falla o tarda, la IA se pausa 30 s para no frenar las señales siguientes (la alerta ya salió)."""
    global _IA_PAUSA_HASTA
    if time.time() < _IA_PAUSA_HASTA:
        raise ErrorLMStudio("IA en pausa tras un fallo reciente")
    try:
        asegurar_modelo(callback_log)
        modelo = _modelo_a_usar()
    except ErrorLMStudio:
        _IA_PAUSA_HASTA = time.time() + IA_PAUSA_TRAS_FALLO_S
        raise
    candidatos = candidatos_para_ia(c, etiqueta_h)
    esquema = json.loads(json.dumps(ESQUEMA_JSON))
    esquema["properties"]["categoria"]["enum"] = candidatos
    mensajes = [
        {"role": "system", "content": PROMPT_SISTEMA},
        {"role": "user", "content": mensaje_para_ia(_resumen_para_ia(c, etiqueta_h, confianza_h, contexto))
                                    + f"\nOpciones válidas para 'categoria' (elige SOLO una de estas): {', '.join(candidatos)}."}
    ]
    with IA_SEM:
        t0 = time.perf_counter()
        texto, fin = _consultar(modelo, mensajes, 130, LMSTUDIO_TIMEOUT_S, esquema)
        try:
            datos = _extraer_json(texto)
        except ErrorLMStudio as e:
            restante = LMSTUDIO_TIMEOUT_S - (time.perf_counter() - t0)
            if restante < 1.0:
                raise
            if callback_log:
                callback_log(f"  IA: {e} (fin={fin}); se reintenta una vez pidiendo solo el JSON.")
            reintento = mensajes + [{"role": "assistant", "content": str(texto)[-400:]},
                                    {"role": "user", "content": "Responde únicamente el objeto JSON pedido, sin texto adicional."}]
            texto, fin = _consultar(modelo, reintento, 400 if fin == "length" else 160, restante, esquema)
            datos = _extraer_json(texto)
        t_ia = time.perf_counter() - t0
    resultado = _normalizar(datos)
    if resultado["categoria"] not in candidatos:          # modelos sin formato estricto pueden salirse de la lista
        resultado["justificacion"] = f"(la IA propuso '{resultado['categoria']}', fuera de las opciones) " + resultado["justificacion"]
        resultado["categoria"], resultado["confianza"] = etiqueta_h, min(resultado["confianza"], 0.5)
    resultado["candidatos"] = candidatos
    resultado["modelo"] = modelo
    resultado["t_ia_s"] = round(t_ia, 3)
    return resultado

def _nivel(categoria, confianza):
    if categoria in CATEGORIAS_AMENAZA:
        return "alta" if confianza >= 0.80 else "media" if confianza >= 0.60 else "baja"
    if categoria == "motor_vehiculo":
        return "media" if confianza >= 0.60 else "baja"
    return "ninguna" if categoria == "ambiente_natural" else "baja"

ACCIONES = {
    "motosierra": "Enviar patrulla al sensor: posible tala ilegal.",
    "disparo": "Enviar patrulla con precaución: posible caza ilegal.",
    "incendio": "Verificar humo o fuego cerca del sensor y avisar a bomberos si se confirma.",
    "motor_vehiculo": "Verificar presencia de vehículo no autorizado.",
    "ambiente_natural": "Sin acción necesaria.",
    "otro": "Registrar el evento y monitorear repeticiones.",
}

def _fusionar(c, heuristica, ia):
    """Decisión final entre la etapa acústica (reglas + red) y la IA de texto.
    La IA de texto no oye el audio: puede confirmar, bajar a revisión una alerta débil o subir un caso dudoso que la
    red respalde en parte, pero nunca crea una amenaza sin evidencia acústica ni anula una detección concluyente."""
    h, hc = heuristica["categoria"], heuristica["confianza"]
    i, ic = ia["categoria"], ia["confianza"]
    red = (c.get("red") or {}).get("categorias", {})
    apoyo_red = red.get(i, 0.0) if i in red else 0.0
    # La IA contradice evidencia clara de la red (p. ej. dice "disparo" cuando la red oye motosierra al 80 % y
    # disparo al 0 %): respuesta incoherente de un modelo débil, se ignora.
    red_cat = {k: v for k, v in red.items() if k in CATEGORIAS}
    red_mejor = max(red_cat, key=red_cat.get) if red_cat else None
    incoherente = bool(red_cat) and i != h and apoyo_red < 0.10 and i in red_cat and (
        red.get(h, 0.0) >= 0.45 or (red_cat[red_mejor] >= 0.50 and i != red_mejor))

    rh = red.get(h, 0.0)
    if h not in CATEGORIAS_AMENAZA and i in CATEGORIAS_AMENAZA and ic >= 0.80 and apoyo_red >= 0.30 and not incoherente:
        # prioridad: no perder una amenaza real. La IA la ve y la red la respalda: se alerta aunque lo acústico dudara.
        categoria, confianza, fuente = i, round(min(0.85, 0.5 * ic + 0.5 * apoyo_red + 0.1), 3), "ia_rescata_amenaza"
    elif _es_concluyente(c, h, hc):
        categoria, confianza, fuente = h, hc, "acustica_concluyente"
    elif incoherente:
        categoria, confianza, fuente = h, hc, "ia_incoherente_ignorada"
    elif i == h:
        categoria, confianza, fuente = h, round(min(0.98, 0.6 * hc + 0.4 * ic + 0.05), 3), "fusion_coinciden"
    # --- la IA propone OTRA amenaza: solo si la red la respalda más que la actual -------------------------------
    elif h in CATEGORIAS_AMENAZA and i in CATEGORIAS_AMENAZA:
        if apoyo_red >= 0.30 and apoyo_red >= rh + 0.10:
            categoria, confianza, fuente = i, round(min(0.9, 0.5 * ic + 0.5 * apoyo_red), 3), "ia_corrige_tipo_amenaza"
        else:
            categoria, confianza, fuente = h, hc, "acustica_mantiene_tipo"
    # --- la IA ve una amenaza que lo acústico dejó en duda: solo con respaldo de la red --------------------------
    elif i in CATEGORIAS_AMENAZA and ic >= 0.70 and (apoyo_red >= 0.25 or (heuristica.get("puntaje_regla", 0) >= 0.5 and apoyo_red >= 0.10)):
        categoria, confianza, fuente = i, round(min(0.5 * ic + 0.5 * max(apoyo_red, hc * 0.8), 0.85), 3), "fusion_ia_con_apoyo_acustico"
    # --- la IA dice que una alerta NO es amenaza: solo se acepta si la alerta era débil ---------------------------
    #     (para que una equivocación de la IA nunca haga perder una alerta verdadera)
    #     La IA nunca convierte una alerta en "no amenaza": como mucho deja una nota para revisarla.
    elif h in CATEGORIAS_AMENAZA and i not in CATEGORIAS_AMENAZA:
        if ic >= 0.80 and hc < 0.75 and rh < 0.15:
            categoria, confianza, fuente = h, hc, "ia_sugiere_revisar"
        else:
            categoria, confianza, fuente = h, hc, "acustica_mantiene_alerta"
    elif i not in CATEGORIAS_AMENAZA and h not in CATEGORIAS_AMENAZA and (ic > hc or h == "otro"):
        categoria, confianza, fuente = i, round(0.7 * ic + 0.2 * hc, 3), "fusion_ia"
    else:
        categoria, confianza, fuente = h, hc, "fusion_acustica"

    confianza = float(np.clip(confianza, 0.0, 0.98))
    if categoria == i:
        justificacion, accion = ia.get("justificacion") or "", ia.get("accion") or ACCIONES.get(categoria, "")
    else:   # se impuso la evidencia acústica: la acción de la IA correspondía a otra categoría
        justificacion = f"IA sugirió {i} ({ic:.0%}: {ia.get('justificacion', '')}); se mantiene {categoria} por la evidencia acústica"
        accion = ACCIONES.get(categoria, "")
    return {
        "categoria": categoria,
        "confianza": round(confianza, 3),
        "nivel_alerta": _nivel(categoria, confianza),
        "justificacion": justificacion + (f" | {heuristica.get('explicacion', '')}" if heuristica.get("explicacion") else ""),
        "accion": accion,
        "fuente": fuente,
        "modelo": ia.get("modelo"),
        "t_ia_s": ia.get("t_ia_s", 0.0)
    }

def _resultado_reglas(etiqueta, confianza, scores, explicacion=""):
    """Resultado de la etapa acústica (reglas + red neuronal), antes de la IA de texto."""
    return {
        "categoria": etiqueta,
        "confianza": confianza,
        "nivel_alerta": _nivel(etiqueta, confianza),
        "justificacion": explicacion or "Clasificación acústica.",
        "explicacion": explicacion,
        "accion": ACCIONES.get(etiqueta, ""),
        "fuente": "acustica+red" if "Red:" in (explicacion or "") else "heuristico",
        "modelo": None,
        "t_ia_s": 0.0,
        "scores_heuristicos": scores
    }

def debe_alertar(resultado):
    return resultado["categoria"] in CATEGORIAS_AMENAZA and resultado["confianza"] >= UMBRAL_CONFIANZA_ALERTA

def emitir_alerta(analisis_id, audio, resultado, t_inicio_perf):
    nivel = resultado["nivel_alerta"]
    if nivel not in {"media", "alta"}:
        nivel = "media"
    t_alerta = round(time.perf_counter() - t_inicio_perf, 3)
    mensaje = (
        f"ALERTA {nivel.upper()}: {resultado['categoria'].upper()} detectada en '{audio['nombre_archivo']}' "
        f"(Confianza: {resultado['confianza']:.0%}, Envío: {t_alerta:.3f}s). {resultado.get('accion', '')}"
    ).strip()
    alerta_id = guardar_alerta(analisis_id, audio["sensor_id"], audio["latitud"], audio["longitud"], nivel, mensaje, t_alerta)
    return mensaje, t_alerta, alerta_id, nivel

def _es_concluyente(c, h, hc):
    """True si la etapa acústica ya es concluyente: en modo 'auto' no se consulta a la IA de texto (más rápido) y
    _fusionar no deja que la IA la cambie. Con red neuronal: reglas y red de acuerdo con confianza alta."""
    red = (c.get("red") or {}).get("categorias")
    if red:
        red_cat = {k: v for k, v in red.items() if k in CATEGORIAS}
        mejor = max(red_cat, key=red_cat.get) if red_cat else None
        if mejor == h and h != "otro" and red_cat[mejor] >= 0.60:
            return True        # la red reconoce el sonido con claridad y coincide: la IA no aporta (ahorra tokens)
        if h in CATEGORIAS_AMENAZA:
            return hc >= 0.85 and red.get(h, 0.0) >= 0.30
        return hc >= 0.85
    segs = c.get("segmentos", [])
    shot = max(_shot_score(c), max((_shot_score(s) for s in segs), default=0.0))
    chain = max(_chainsaw_score(c), max((_chainsaw_score(s) for s in segs), default=0.0))
    vehicle = max(_vehicle_score(c), max((_vehicle_score(s) for s in segs), default=0.0))
    ambiental = h == "ambiente_natural" and hc >= 0.68 and (c.get("flatness_avg", 0) >= 0.50 or c.get("periodic_avg", 0) < 0.20)
    return _es_hard_shot(c, shot) or _es_hard_chain(c, chain) or _es_hard_vehicle(c, vehicle) or ambiental

class YaTomado(Exception):
    """Otro hilo ya está analizando (o analizó) este audio."""

def _reclamar_audio(audio_id):
    with conectar() as bd:
        cur = bd.execute("UPDATE audios SET estado='procesando' WHERE id=? AND estado IN ('pendiente','en_cola')", (audio_id,))
        return cur.rowcount == 1

def _anotar_revision(alerta_id, categoria, confianza):
    texto = (f"REVISAR: el análisis con IA reclasificó la señal como {categoria.upper()} ({confianza:.0%}); "
             f"posible falsa alarma.")
    with conectar() as bd:
        bd.execute("UPDATE alertas SET revision=?, mensaje=mensaje || ' | ' || ? WHERE id=?", (texto, texto, alerta_id))
        fila = bd.execute("SELECT sms_enviado FROM alertas WHERE id=?", (alerta_id,)).fetchone()
    return texto, bool(fila and fila["sms_enviado"])

def _emitir_y_avisar(analisis_id, audio, res, t_inicio_perf, log, origen="acustica"):
    """Guarda la alerta y avisa a los encargados (Telegram/SMS). Devuelve (mensaje, t_alerta, alerta_id)."""
    mensaje, t_alerta, alerta_id, nivel = emitir_alerta(analisis_id, audio, res, t_inicio_perf)
    with conectar() as bd:
        bd.execute("UPDATE alertas SET origen=? WHERE id=?", (origen, alerta_id))
    try:
        estado_sms = notificar_alerta(alerta_id, audio, res["categoria"], res["confianza"], nivel)
        t_alerta = round(time.perf_counter() - t_inicio_perf, 3)
        with conectar() as bd:
            bd.execute("UPDATE alertas SET t_alerta_s=? WHERE id=?", (t_alerta, alerta_id))
        log(f"  {estado_sms}")
    except Exception as e:
        log(f"  No se pudo enviar el aviso: {e}")
    return mensaje, t_alerta, alerta_id

def analizar_audio(audio, usar_ia=True, callback_log=None, ia_sincrona=False):
    """Etapa 1 (rápida, < 1 s): reglas acústicas + red neuronal que escucha el audio -> si hay amenaza, alerta y aviso
    a los encargados DE INMEDIATO (no depende de la IA).
    Etapa 2: el análisis entra en la COLA DE IA; un único trabajador lo envía a LM Studio cuando le toque (uno por uno),
    sin descartarlo si LM Studio está ocupado o caído. Con ia_sincrona=True se espera aquí a la IA (pruebas, consola)."""
    t_inicio_perf = time.perf_counter()
    if not _reclamar_audio(audio["id"]):
        raise YaTomado(audio["id"])
    log = callback_log or (lambda m: None)
    hora_inicio = _hora_ms()
    hilo_nombre = threading.current_thread().name
    try:
        t0 = time.perf_counter()
        x = cargar_audio(audio["ruta"])
        c, etiqueta_h, confianza_h, scores_h, explicacion = analisis_acustico(x)
        t_audio = round(time.perf_counter() - t0, 3)
        c["decision_acustica"] = {"categoria": etiqueta_h, "confianza": confianza_h, "explicacion": explicacion,
                                  "puntaje_regla": max([v for k, v in (scores_h or {}).items() if k in CATEGORIAS_AMENAZA] or [0.0])}
        red_txt = ""
        if c.get("red"):
            red_txt = " | Red: " + ", ".join(f"{n} {p:.0%}" for n, p in c["red"]["top"][:3])
            if c["red"].get("ganancia_db"):
                red_txt += f" (+{c['red']['ganancia_db']:.0f} dB)"
        log(f"[{hora_inicio}] [{hilo_nombre}] Analizando '{audio['nombre_archivo']}' -> "
            f"Acústica: {etiqueta_h} ({confianza_h:.0%}) en {t_audio:.3f}s{red_txt}")
        resultado = _resultado_reglas(etiqueta_h, confianza_h, scores_h, explicacion)
        resultado.update({
            "duracion_audio_s": c["duracion_s"],
            "t_audio_s": t_audio,
            "caracteristicas": json.dumps(c, ensure_ascii=False),
            "etiqueta_heuristica": etiqueta_h,
            "hilo": hilo_nombre,
            "hora_inicio": hora_inicio
        })
        analisis_id = guardar_analisis(audio["id"], resultado)
        marcar_audio(audio["id"], "analizado")
    except Exception:
        marcar_audio(audio["id"], "error")
        raise

    alerta_id, mensaje, t_alerta = None, None, None
    if debe_alertar(resultado):                             # la alerta sale ANTES de la IA
        mensaje, t_alerta, alerta_id = _emitir_y_avisar(analisis_id, audio, resultado, t_inicio_perf, log)

    cfg = cargar_config()
    if usar_ia and cfg.get("ia_activa", True):
        if not (IA_MODO == "siempre" or cfg.get("ia_todas")) and _es_concluyente(c, etiqueta_h, confianza_h):
            log(f"  [{hilo_nombre}] IA no necesaria: detección acústica concluyente "
                f"(activa «IA en todas» en Administración para revisarla igual).")
        else:
            with conectar() as bd:
                bd.execute("UPDATE analisis SET ia_estado='pendiente', ia_intentos=0 WHERE id=?", (analisis_id,))
            resultado["ia_estado"] = "pendiente"
            if ia_sincrona:
                COLA_IA.procesar_uno(analisis_id, log)
                with conectar() as bd:
                    fila = bd.execute("SELECT * FROM analisis WHERE id=?", (analisis_id,)).fetchone()
                resultado.update({k: fila[k] for k in ("categoria", "confianza", "nivel_alerta", "justificacion",
                                                       "accion", "fuente", "modelo", "t_ia_s", "ia_estado")})
            else:
                n = COLA_IA.despertar()
                log(f"  [{hilo_nombre}] En cola para la IA ({n} en espera).")
    resultado["t_alerta_s"] = t_alerta
    return resultado, mensaje

# ======================================================================================
# COLA DE IA: una consulta a la vez, nada se pierde
# ======================================================================================
IA_REINTENTOS = 4                    # intentos por audio si LM Studio responde mal
IA_MAX_ESPERA_H = 6.0                # audios más viejos que esto ya no se envían a la IA (ya pasó el momento)

class ColaIA:
    """Lista de espera persistente (en la base de datos) de análisis que debe revisar la IA. Un solo hilo los envía a
    LM Studio de uno en uno, en orden de llegada. Si LM Studio está apagado espera y reintenta; si responde mal,
    reintenta hasta IA_REINTENTOS veces. El resultado acústico y las alertas ya salieron antes, así que esperar no
    retrasa ningún aviso."""

    def __init__(self):
        self._hilo = None
        self._evento = threading.Event()
        self._lock = threading.Lock()
        self.oyentes = []            # funciones log(texto) y refresco() de la ventana o del servidor
        self.trabajando_en = None

    def _log(self, texto):
        for o in list(self.oyentes):
            try:
                o[0](texto)
            except Exception:
                pass

    def _refrescar(self):
        for o in list(self.oyentes):
            try:
                o[1]()
            except Exception:
                pass

    def registrar(self, log, refresco=lambda: None):
        self.oyentes.append((log, refresco))

    def pendientes(self):
        try:
            with conectar() as bd:
                return bd.execute("SELECT COUNT(*) FROM analisis WHERE ia_estado='pendiente'").fetchone()[0]
        except Exception:
            return 0

    def despertar(self):
        with self._lock:
            if self._hilo is None or not self._hilo.is_alive():
                self._hilo = threading.Thread(target=self._bucle, name="Cola-IA", daemon=True)
                self._hilo.start()
        self._evento.set()
        return self.pendientes()

    def esperar(self, timeout=None):
        """Espera a que la cola quede vacía (consola: para no cerrar el programa antes de que la IA termine)."""
        fin = time.time() + (timeout or 1e9)
        while time.time() < fin and self.pendientes():
            time.sleep(0.5)

    def en_espera(self):
        """Motivo por el que la cola no envía prompts ahora (None = puede enviar)."""
        cfg = cargar_config()
        if not cfg.get("ia_activa", True):
            return "IA desactivada"
        permitido, motivo = estado_analisis(cfg)
        if not permitido:
            return "análisis pausado" if "pausado" in motivo else "fuera de horario"
        return None

    def _audio_en_proceso(self):
        try:
            with conectar() as bd:
                return bd.execute("SELECT COUNT(*) FROM audios WHERE estado IN ('en_cola','procesando')").fetchone()[0]
        except Exception:
            return 0

    def _bucle(self):
        while True:
            if self.en_espera():               # IA apagada, análisis pausado o fuera de horario: ningún prompt
                self._evento.wait(5); self._evento.clear()
                continue
            # Prioridad a la etapa acústica: LM Studio en CPU compite por el procesador y hacía que el análisis
            # (y la alerta) tardara varios segundos. Si hay audios por analizar, la IA espera (máx. 20 s).
            for _ in range(20):
                if not self._audio_en_proceso():
                    break
                time.sleep(1)
            with conectar() as bd:
                limite = time.time() - IA_MAX_ESPERA_H * 3600
                viejos = bd.execute("UPDATE analisis SET ia_estado='omitida' WHERE ia_estado='pendiente' AND audio_id IN "
                                    "(SELECT id FROM audios WHERE t_subida<?)", (limite,)).rowcount
                fila = bd.execute("SELECT id FROM analisis WHERE ia_estado='pendiente' ORDER BY id LIMIT 1").fetchone()
            if viejos:
                self._log(f"🧠 {viejos} análisis con más de {IA_MAX_ESPERA_H:.0f} h en espera se omitieron para la IA.")
            if not fila:
                self._evento.wait(30); self._evento.clear()
                continue
            espera = self.procesar_uno(fila["id"], self._log)
            self._refrescar()
            if espera:
                self._evento.wait(espera); self._evento.clear()

    def procesar_uno(self, analisis_id, log):
        """Envía UN análisis a la IA y aplica el resultado. Devuelve segundos a esperar antes del siguiente (0 = seguir)."""
        global _IA_PAUSA_HASTA
        with conectar() as bd:
            f = bd.execute("SELECT an.*, au.sensor_id, au.nombre_archivo, au.t_subida, au.ruta, au.id AS aid, "
                           "s.latitud, s.longitud FROM analisis an JOIN audios au ON au.id=an.audio_id "
                           "JOIN sensores s ON s.id=au.sensor_id WHERE an.id=?", (analisis_id,)).fetchone()
        if not f or f["ia_estado"] != "pendiente":
            return 0
        if not cargar_config().get("ia_activa", True):      # se apagó la IA mientras esperaba: no se envía
            with conectar() as bd:
                bd.execute("UPDATE analisis SET ia_estado='omitida' WHERE id=?", (analisis_id,))
            return 0
        if self.en_espera():                                 # análisis pausado o fuera de horario: se queda en cola
            return 5
        self.trabajando_en = analisis_id
        try:
            c = json.loads(f["caracteristicas"] or "{}")
            da = c.get("decision_acustica") or {"categoria": f["etiqueta_heuristica"], "confianza": f["confianza"]}
            audio = {"id": f["aid"], "sensor_id": f["sensor_id"], "nombre_archivo": f["nombre_archivo"],
                     "t_subida": f["t_subida"], "latitud": f["latitud"], "longitud": f["longitud"], "ruta": f["ruta"]}
            _IA_PAUSA_HASTA = 0.0                       # la cola maneja sus propias esperas
            try:
                ia = clasificar_con_ia(c, da["categoria"], da["confianza"], None, log, contexto_para_ia(audio))
            except ErrorLMStudio as e:
                caido = "no está disponible" in str(e) or "No se pudo conectar" in str(e)
                if caido:
                    if not getattr(self, "_avisado_caido", False):
                        self._avisado_caido = True
                        log(f"🧠 LM Studio no responde: {self.pendientes()} análisis esperan en la cola y se enviarán "
                            f"cuando vuelva (las alertas ya salieron).")
                    return 15
                self._avisado_caido = False
                with conectar() as bd:
                    if caido:                           # servidor apagado: no gasta intentos, espera a que vuelva
                        intentos = f["ia_intentos"] or 0
                    else:
                        intentos = (f["ia_intentos"] or 0) + 1
                        bd.execute("UPDATE analisis SET ia_intentos=? WHERE id=?", (intentos, analisis_id))
                    if intentos >= (2 if "tardó más de" in str(e) else IA_REINTENTOS):
                        bd.execute("UPDATE analisis SET ia_estado='fallo' WHERE id=?", (analisis_id,))
                        log(f"🧠 IA: '{f['nombre_archivo']}' sin respuesta válida tras {intentos} intentos ({e}). "
                            f"Se mantiene el resultado acústico.")
                        return 2
                log(f"🧠 IA ocupada o sin respuesta ({e}). '{f['nombre_archivo']}' sigue en la cola"
                    f"{'' if caido else f' (intento {intentos}/{IA_REINTENTOS})'}.")
                if "tardó más de" in str(e):
                    # LM Studio sigue generando la respuesta anterior aunque aquí se haya agotado la espera:
                    # reenviar enseguida solo acumula prompts ("2 GEN" en LM Studio). Se le da tiempo a terminar.
                    return 60
                return 15 if caido else 5 * max(1, intentos)
            if getattr(self, "_avisado_caido", False):
                self._avisado_caido = False
                log("🧠 LM Studio volvió a responder: se reanuda la cola.")
            final = _fusionar(c, da, ia)
            actualizar_analisis(analisis_id, final)
            with conectar() as bd:
                bd.execute("UPDATE analisis SET ia_estado='hecho' WHERE id=?", (analisis_id,))
                alerta = bd.execute("SELECT * FROM alertas WHERE analisis_id=? ORDER BY id LIMIT 1", (analisis_id,)).fetchone()
            log(f"🧠 IA ({ia['t_ia_s']:.1f}s) '{f['nombre_archivo']}': {ia['categoria']} {ia['confianza']:.0%} - "
                f"{ia['justificacion']} -> final: {final['categoria'].upper()} ({final['fuente']})")
            if alerta is not None and final["fuente"] == "ia_sugiere_revisar":
                nota = f"La IA sugiere revisar: podría ser {ia['categoria']} ({ia['confianza']:.0%}). La alerta se mantiene."
                with conectar() as bd:
                    bd.execute("UPDATE alertas SET revision=?, mensaje=mensaje || ' | ' || ? WHERE id=?", (nota, nota, alerta["id"]))
                log(f"  ℹ {nota}")
            elif alerta is not None and not debe_alertar(final):
                texto, hubo_sms = _anotar_revision(alerta["id"], final["categoria"], final["confianza"])
                log(f"  ⚠ {texto}")
                if hubo_sms:
                    try:
                        notificar_revision(alerta["id"], audio, final["categoria"])
                    except Exception as e:
                        log(f"  No se pudo enviar el aviso de revisión: {e}")
            elif alerta is None and debe_alertar(final):
                t0 = time.perf_counter() - max(0.0, time.time() - float(f["t_subida"] or time.time()))
                _emitir_y_avisar(analisis_id, audio, final, t0, log, origen="ia")
                log("  🚨 La IA confirmó una amenaza que la etapa acústica dejó en duda: alerta enviada.")
            return 0
        except Exception as e:                          # nunca debe morir el hilo de la cola
            with conectar() as bd:
                bd.execute("UPDATE analisis SET ia_estado='fallo' WHERE id=?", (analisis_id,))
            log(f"🧠 IA: error inesperado con el análisis #{analisis_id}: {e}")
            return 2
        finally:
            self.trabajando_en = None

COLA_IA = ColaIA()

def procesar_pendientes_paralelo(usar_ia=True, max_workers=4, callback_log=None, callback_progreso=None):
    nuevos = escanear_entrada()
    pendientes = audios_pendientes()

    if not pendientes:
        if callback_log:
            callback_log("No hay señales pendientes de análisis.")
        return {"procesados": 0, "t_paralelo_s": 0.0, "t_secuencial_s": 0.0, "workers": max_workers}

    if usar_ia:
        try:
            _modelo_a_usar.cache_clear()
            modelo_activo = _modelo_a_usar()
            if callback_log:
                callback_log(f"LM Studio conectado. Modelo activo: {modelo_activo}")
        except ErrorLMStudio as e:
            if callback_log:   # ya no se apaga la IA: los análisis quedan en cola hasta que LM Studio responda
                callback_log(f"Aviso LM Studio: {e} -> las alertas salen igual; la revisión de la IA queda en cola.")

    n_total = len(pendientes)
    workers_reales = max(2, min(int(max_workers), max(2, n_total)))
    if callback_log:
        callback_log(
            f"Iniciando ejecución simultánea de {n_total} señal(es) usando {workers_reales} hilos en paralelo..."
        )

    t_lote_ini = time.perf_counter()
    suma_individual = 0.0
    completados = 0
    info_hilos = {}

    with ThreadPoolExecutor(max_workers=workers_reales, thread_name_prefix="Señal-Worker") as pool:
        futuros = {
            pool.submit(analizar_audio, dict(audio), usar_ia, callback_log): dict(audio)
            for audio in pendientes
        }
        for fut in as_completed(futuros):
            audio = futuros[fut]
            completados += 1
            try:
                resultado, mensaje = fut.result()
                t_ind = (resultado.get("t_audio_s") or 0.0) + (resultado.get("t_ia_s") or 0.0)
                suma_individual += t_ind
                info_hilos[audio["id"]] = f"{resultado.get('hilo', 'Worker')} ({resultado.get('hora_inicio', '')})"
                if callback_log:
                    alerta_txt = f" | Envío Alerta: {resultado['t_alerta_s']:.3f}s" if resultado.get("t_alerta_s") is not None else ""
                    callback_log(
                        f"✓ [{resultado.get('hilo')}] '{audio['nombre_archivo']}' -> {resultado['categoria'].upper()} "
                        f"({resultado['confianza']:.0%}) | Detección Acústica: {resultado['t_audio_s']:.3f}s | "
                        f"IA: {resultado['t_ia_s']:.3f}s{alerta_txt}"
                    )
                    if mensaje:
                        callback_log(f"  🚨 {mensaje}")
            except YaTomado:
                pass
            except Exception as e:
                marcar_audio(audio["id"], "error")
                if callback_log:
                    callback_log(f"✗ Error en '{audio['nombre_archivo']}': {e}")
            if callback_progreso:
                callback_progreso(completados, n_total)

    t_paralelo = round(time.perf_counter() - t_lote_ini, 3)
    t_secuencial = round(max(suma_individual, t_paralelo), 3)
    if callback_log:
        acel = (t_secuencial / t_paralelo) if t_paralelo > 0 else 1.0
        callback_log(
            f"=== Lote finalizado: {n_total} señales en {t_paralelo:.3f}s (Secuencial estimado: {t_secuencial:.3f}s | {acel:.2f}x velocidad) ==="
        )

    return {
        "procesados": n_total,
        "t_paralelo_s": t_paralelo,
        "t_secuencial_s": t_secuencial,
        "workers": workers_reales,
        "info_hilos": info_hilos
    }

def procesar_pendientes(usar_ia=True):
    if print not in [o[0] for o in COLA_IA.oyentes]:
        COLA_IA.registrar(print)
    res = procesar_pendientes_paralelo(usar_ia=usar_ia, max_workers=4, callback_log=print)
    if usar_ia and COLA_IA.pendientes():
        print(f"Esperando a que la IA revise {COLA_IA.pendientes()} análisis en cola (Ctrl+C para salir)...")
        try:
            COLA_IA.esperar()
        except KeyboardInterrupt:
            print("Quedan en cola; se revisarán la próxima vez que se abra el programa.")
    return res["procesados"]

def distancia_km(a, b):
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))

def ruta_optima(origen, paradas):
    paradas = list(paradas)[:MAX_PARADAS]
    n = len(paradas)
    if n == 0:
        return [], 0.0
    coords = [(p[1], p[2]) for p in paradas]
    d = [[distancia_km(a, b) for b in coords] for a in coords]
    desde_origen = [distancia_km(origen, c) for c in coords]
    todas = (1 << n) - 1

    @lru_cache(maxsize=None)
    def mejor(mask, ultima):
        if mask == todas:
            return distancia_km(coords[ultima], origen), -1
        return min((d[ultima][j] + mejor(mask | (1 << j), j)[0], j) for j in range(n) if not mask & (1 << j))

    total, primera = min((desde_origen[j] + mejor(1 << j, j)[0], j) for j in range(n))
    orden = [paradas[primera][0]]
    mask = 1 << primera
    actual = primera
    while mask != todas:
        _, sig = mejor(mask, actual)
        orden.append(paradas[sig][0])
        mask |= 1 << sig
        actual = sig
    return orden, total

def mostrar_ruta_patrullaje():
    activas = alertas_activas()
    if not activas:
        print("No hay alertas activas.")
        return
    sensores = {}
    for alerta in activas:
        sensores.setdefault(alerta["sensor_id"], (alerta["sensor_id"], alerta["latitud"], alerta["longitud"]))
    nombre, lat, lon = PUESTO_GUARDABOSQUES
    orden, km = ruta_optima((lat, lon), list(sensores.values()))
    print(f"\nRuta óptima ({len(orden)} parada(s), {km:.2f} km):")
    print(" -> ".join([nombre] + orden + [nombre]))

# ======================================================================================
# SMS, SENSORES Y RECEPCIÓN DE AUDIO EN TIEMPO REAL
# ======================================================================================
SENSOR_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
CONFIG_DEFECTO = {
    "sms_proveedor": "simulado",      # simulado | android | telegram | twilio | webhook
    "sms_enfriamiento_s": 60,         # no repetir SMS del mismo sensor antes de este tiempo
    "sms_nivel_minimo": "media",      # media | alta
    "twilio_sid": "", "twilio_token": "", "twilio_from": "",
    "webhook_url": "",
    # SMS Gateway for Android (gratis): el celular envía los SMS con su propio plan
    "android_url": "", "android_usuario": "", "android_clave": "",
    # Telegram (gratis, por internet): bot creado con @BotFather y chats/grupos de destino
    "telegram_token": "", "telegram_chats": "",
    "servidor_puerto": 8000,
    "servidor_red": False,            # True = aceptar sensores de otros equipos de la red
    "servidor_puerto_https": 8443,
    "servidor_encendido": False,      # se recuerda: al abrir el programa, el servidor de sensores se enciende solo    # para celulares: el navegador solo deja usar el micrófono con HTTPS
    # ---- Administración (los cambia un administrador con PIN) ----
    "analisis_activo": True,          # False = no se analiza el audio que llega (ni acústica ni IA)
    "ia_activa": True,                # False = no se consulta LM Studio (ahorra tokens); la etapa acústica sigue
    "ia_todas": True,                 # True = la IA revisa TODAS las señales (en cola, una por una); False = solo dudosas
    "horario_activo": False,          # True = analizar solo dentro del horario
    "horario_inicio": "18:00",
    "horario_fin": "06:00",
    "solo_con_sonido": False,         # True = los sensores solo envían audio cuando escuchan algo (ahorra red y análisis)
    "sensibilidad_sonido": "media",   # alta | media | baja: cuánto debe sobresalir el sonido del ruido de fondo
    "admin_pin": "",                  # PIN de administrador (guardado como hash con sal)
}
_CFG_LOCK = threading.Lock()

def cargar_config():
    cfg = dict(CONFIG_DEFECTO)
    try:
        cfg.update(json.loads(ARCHIVO_CONFIG.read_text(encoding="utf-8")))
    except Exception:
        pass
    for k, v in (("twilio_sid", "TWILIO_ACCOUNT_SID"), ("twilio_token", "TWILIO_AUTH_TOKEN"), ("twilio_from", "TWILIO_FROM")):
        if os.environ.get(v):
            cfg[k] = os.environ[v]
    return cfg

def guardar_config(nuevo):
    if nuevo.get("twilio_from"):
        nuevo = dict(nuevo, twilio_from=normalizar_emisor(nuevo["twilio_from"]))   # error claro al guardar, no al enviar
    if nuevo.get("twilio_sid"):
        nuevo = dict(nuevo, twilio_sid=normalizar_sid(nuevo["twilio_sid"]))
    with _CFG_LOCK:
        cfg = cargar_config()
        for k in CONFIG_DEFECTO:
            if k in nuevo:
                cfg[k] = nuevo[k]
        ARCHIVO_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg

# ------------------------------------------------------------------ administración
def _hash_pin(pin, sal=None):
    sal = sal or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), bytes.fromhex(sal), 120_000).hex()
    return f"{sal}${h}"

def hay_pin():
    return bool(cargar_config().get("admin_pin"))

def verificar_pin(pin):
    guardado = cargar_config().get("admin_pin") or ""
    if not guardado:
        return True                                   # sin PIN configurado: cualquiera puede administrar
    sal, _, _ = guardado.partition("$")
    return hmac.compare_digest(_hash_pin(pin or "", sal), guardado)

def cambiar_pin(pin_actual, pin_nuevo):
    if not verificar_pin(pin_actual):
        raise ValueError("El PIN actual no es correcto.")
    pin_nuevo = str(pin_nuevo or "").strip()
    if pin_nuevo and (len(pin_nuevo) < 4 or not pin_nuevo.isdigit()):
        raise ValueError("El PIN debe tener al menos 4 dígitos.")
    with _CFG_LOCK:
        cfg = cargar_config()
        cfg["admin_pin"] = _hash_pin(pin_nuevo) if pin_nuevo else ""
        ARCHIVO_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

def _hhmm(texto):
    m_ = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(texto or ""))
    if not m_ or int(m_.group(1)) > 23 or int(m_.group(2)) > 59:
        raise ValueError(f"Hora inválida: '{texto}'. Usa el formato HH:MM, por ejemplo 18:00.")
    return int(m_.group(1)) * 60 + int(m_.group(2))

def en_horario(inicio, fin, ahora=None):
    """True si la hora actual está dentro del horario. Admite horarios que cruzan la medianoche (18:00-06:00)."""
    t = time.localtime(ahora or time.time())
    m_ = t.tm_hour * 60 + t.tm_min
    a, b = _hhmm(inicio), _hhmm(fin)
    if a == b:
        return True
    return a <= m_ < b if a < b else (m_ >= a or m_ < b)

def estado_analisis(cfg=None):
    """(permitido, motivo). Lo consultan el servidor de sensores y la vigilancia de carpeta antes de analizar."""
    cfg = cfg or cargar_config()
    if not cfg.get("analisis_activo", True):
        return False, "análisis pausado por un administrador"
    if cfg.get("horario_activo"):
        try:
            if not en_horario(cfg["horario_inicio"], cfg["horario_fin"]):
                return False, f"fuera del horario de monitoreo ({cfg['horario_inicio']}-{cfg['horario_fin']})"
        except ValueError:
            pass
    return True, "activo"

def administrar(pin, **cambios):
    """Cambia la configuración de administración si el PIN es correcto. Claves: analisis_activo, ia_activa,
    ia_todas, horario_activo, horario_inicio, horario_fin."""
    if not verificar_pin(pin):
        raise PermissionError("PIN de administrador incorrecto.")
    permitidas = {"analisis_activo", "ia_activa", "ia_todas", "horario_activo", "horario_inicio", "horario_fin",
                  "solo_con_sonido", "sensibilidad_sonido"}
    if "sensibilidad_sonido" in cambios and cambios["sensibilidad_sonido"] not in UMBRALES_SONIDO:
        raise ValueError("Sensibilidad inválida: usa alta, media o baja.")
    cambios = {k: v for k, v in cambios.items() if k in permitidas and v is not None}
    for k in ("horario_inicio", "horario_fin"):
        if k in cambios:
            _hhmm(cambios[k])
            cambios[k] = "%02d:%02d" % divmod(_hhmm(cambios[k]), 60)
    antes = cargar_config().get("ia_activa", True)
    cfg = guardar_config(cambios)
    _aplicar_modo_ia(cfg)
    if antes and not cfg.get("ia_activa", True):
        descartar_cola_ia("IA desactivada por un administrador")
    return cfg

# dB que el sonido debe superar al ruido de fondo para que el sensor lo envíe
UMBRALES_SONIDO = {"alta": 6.0, "media": 10.0, "baja": 15.0}
LATIDO_S = 120          # aunque haya silencio, el sensor envía un trozo cada 2 min (sigue vivo y no se pierde nada)

def config_sensores(cfg=None):
    """Lo que necesitan saber los sensores (celular, PC): si envían solo cuando hay sonido y con qué umbral."""
    cfg = cfg or cargar_config()
    sens = cfg.get("sensibilidad_sonido", "media")
    return {"solo_con_sonido": bool(cfg.get("solo_con_sonido", False)), "sensibilidad": sens,
            "umbral_db": UMBRALES_SONIDO.get(sens, 10.0), "latido_s": LATIDO_S, "version_pagina": version_pagina_celular()}

def descartar_cola_ia(motivo):
    """Marca como 'omitida' todo lo que esperaba a la IA: con la IA apagada no se debe enviar ningún prompt,
    ni ahora ni cuando se vuelva a encender. Devuelve cuántos se omitieron."""
    try:
        with conectar() as bd:
            n = bd.execute("UPDATE analisis SET ia_estado='omitida' WHERE ia_estado='pendiente'").rowcount
        if n:
            COLA_IA._log(f"🧠 {motivo}: {n} análisis en cola no se enviarán a la IA.")
            COLA_IA._refrescar()
        return n
    except Exception:
        return 0

def _aplicar_modo_ia(cfg=None):
    global IA_MODO
    cfg = cfg or cargar_config()
    if "IA_MODO" not in os.environ or cfg.get("ia_todas"):
        IA_MODO = "siempre" if cfg.get("ia_todas") else "auto"

def consultas_ia_hoy():
    with conectar() as bd:
        return bd.execute("SELECT COUNT(*) FROM analisis WHERE t_ia_s>0 AND fecha>=?",
                          (time.strftime("%Y-%m-%d 00:00:00"),)).fetchone()[0]

# ------------------------------------------------------------------ destinatarios
def normalizar_telefono(t):
    t = re.sub(r"[\s\-().]", "", str(t))
    if t.startswith("+") and t[1:].isdigit() and 8 <= len(t) <= 16:
        return t
    if t.isdigit() and len(t) == 10 and t.startswith("3"):
        return "+57" + t                      # celular colombiano
    if t.isdigit() and t.startswith("57") and len(t) == 12:
        return "+" + t
    raise ValueError("Teléfono inválido. Usa el formato internacional, por ejemplo +573001234567.")

def agregar_destinatario(nombre, telefono):
    tel = normalizar_telefono(telefono)
    inicializar()
    with conectar() as bd:
        bd.execute("INSERT INTO destinatarios(nombre,telefono) VALUES(?,?) ON CONFLICT(telefono) DO UPDATE SET nombre=excluded.nombre, activo=1",
                   (str(nombre).strip() or tel, tel))
    return tel

def borrar_destinatario(did):
    with conectar() as bd:
        bd.execute("DELETE FROM destinatarios WHERE id=?", (did,))

def listar_destinatarios():
    inicializar()
    with conectar() as bd:
        return [dict(r) for r in bd.execute("SELECT * FROM destinatarios ORDER BY id")]

# ------------------------------------------------------------------ mensajes
def _ascii(texto):
    """Sin tildes ni símbolos: un SMS con caracteres especiales se parte cada 70 caracteres en vez de 160."""
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")

def texto_sms(audio, categoria, confianza, nivel, prefijo=""):
    lat, lon = audio["latitud"], audio["longitud"]
    return _ascii(f"{prefijo}ODS15 ALERTA {nivel.upper()}: {categoria.upper()} {confianza:.0%} en sensor {audio['sensor_id']}. "
                  f"https://maps.google.com/?q={lat:.5f},{lon:.5f}")

def _registrar_sms(alerta_id, telefono, texto, proveedor, estado, detalle):
    with conectar() as bd:
        bd.execute("INSERT INTO sms_log(alerta_id,telefono,mensaje,proveedor,estado,detalle,fecha) VALUES(?,?,?,?,?,?,?)",
                   (alerta_id, telefono, texto, proveedor, estado, str(detalle)[:300], _ahora()))

def normalizar_emisor(t):
    """Número emisor de Twilio en formato E.164 (+código de país). Antes se enviaba tal cual y un número sin '+'
    hacía fallar todos los SMS. También acepta un Messaging Service SID (empieza por 'MG')."""
    t = re.sub(r"[\s\-().]", "", str(t or ""))
    if not t or t.upper().startswith("MG"):
        return t
    if t.startswith("00") and t[2:].isdigit():
        t = "+" + t[2:]
    if t.startswith("+"):
        if not (t[1:].isdigit() and 8 <= len(t) <= 16):
            raise ValueError(f"Número emisor inválido: {t}. Formato: +código de país y número, p. ej. +15551234567.")
        return t
    if t.isdigit() and len(t) == 10 and t.startswith("3"):
        return "+57" + t
    if t.isdigit() and 8 <= len(t) <= 15:
        return "+" + t
    raise ValueError(f"Número emisor inválido: {t}. Formato: +código de país y número, p. ej. +15551234567.")

def normalizar_sid(sid):
    """El Account SID de Twilio es 'AC' + 32 caracteres hexadecimales. Al copiarlo de la consola es común
    dejar fuera el 'AC' o arrastrar espacios; aquí se corrige o se explica el problema."""
    sid = re.sub(r"\s", "", str(sid or ""))
    if not sid:
        return sid
    if re.fullmatch(r"[0-9a-fA-F]{32}", sid):
        return "AC" + sid.lower()
    if re.fullmatch(r"[Aa][Cc][0-9a-fA-F]{32}", sid):
        return "AC" + sid[2:].lower()
    raise ValueError("Account SID inválido: debe empezar con 'AC' y tener 34 caracteres (cópialo completo de "
                     "la página de inicio de console.twilio.com).")

ERRORES_TWILIO = {
    20003: "Account SID o Auth Token incorrectos.",
    20404: "Account SID incorrecto (la cuenta no existe).",
    21211: "El número destino no es válido.",
    21212: "El número emisor no es válido: debe empezar con + y el código de país (p. ej. +15551234567).",
    21266: "El número destino y el emisor son el mismo.",
    21408: "Tu cuenta no tiene permiso para enviar a ese país. Actívalo en Twilio > Messaging > Settings > Geo permissions.",
    21606: "El número emisor no es tuyo en Twilio o no puede enviar SMS.",
    21608: "Cuenta de prueba: el número destino no está verificado. Verifícalo en Twilio > Phone Numbers > Verified Caller IDs.",
    21610: "El destinatario respondió STOP y bloqueó los mensajes de este número.",
    21612: "Twilio no puede enviar SMS entre ese emisor y ese destino (ruta no soportada).",
    21614: "El número destino no es un celular.",
    21659: "El número emisor no es un número de Twilio de tu cuenta.",
    30044: "Cuenta de prueba: el mensaje supera el largo permitido.",
}

def _enviar_twilio(cfg, telefono, texto):
    sid, token = normalizar_sid(cfg["twilio_sid"]), str(cfg["twilio_token"]).strip()
    desde = normalizar_emisor(cfg["twilio_from"])
    if not (sid and token and desde):
        raise RuntimeError("Faltan datos de Twilio (Account SID, Auth Token o número emisor).")
    datos = {"To": telefono, "Body": texto}
    datos["MessagingServiceSid" if desde.upper().startswith("MG") else "From"] = desde
    r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                      auth=(sid, token), data=datos, timeout=15)
    if r.status_code >= 300:
        try:
            err = r.json()
            codigo = int(err.get("code") or 0)
            causa = ERRORES_TWILIO.get(codigo, err.get("message", ""))
            raise RuntimeError(f"Twilio error {codigo}: {causa}"[:300])
        except (ValueError, AttributeError):
            raise RuntimeError(f"Twilio HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("sid", "")

def _enviar_webhook(cfg, telefono, texto):
    if not cfg["webhook_url"]:
        raise RuntimeError("Falta la URL del webhook.")
    r = requests.post(cfg["webhook_url"], json={"to": telefono, "message": texto}, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"Webhook HTTP {r.status_code}: {r.text[:200]}")
    return "ok"

def _enviar_android(cfg, telefono, texto):
    """App gratuita 'SMS Gateway for Android' (sms-gate.app): el celular envía el SMS real con su plan.
    URL local: http://IP_DEL_CELULAR:8080/message  |  nube: https://api.sms-gate.app/3rdparty/v1/message"""
    url = str(cfg.get("android_url") or "").strip()
    if not url:
        raise RuntimeError("Falta la URL de SMS Gateway for Android (la muestra la app, p. ej. http://192.168.1.20:8080).")
    if not url.startswith("http"):
        url = "http://" + url
    if not url.rstrip("/").endswith("/message"):
        url = url.rstrip("/") + "/message"
    try:
        r = requests.post(url, auth=(cfg.get("android_usuario", ""), cfg.get("android_clave", "")),
                          json={"textMessage": {"text": texto}, "phoneNumbers": [telefono]}, timeout=15)
    except requests.exceptions.ConnectionError:
        raise RuntimeError("No se pudo conectar con el celular. Revisa que la app esté en 'Online', que el PC y el "
                           "celular estén en la misma red Wi-Fi y que la IP de la URL sea la que muestra la app.")
    if r.status_code == 401:
        raise RuntimeError("Usuario o contraseña incorrectos: cópialos de la app SMS Gateway.")
    if r.status_code >= 300:
        raise RuntimeError(f"SMS Gateway HTTP {r.status_code}: {r.text[:200]}")
    return f"aceptado por el celular ({r.json().get('id', 'ok') if r.content else 'ok'})"

def _chats_telegram(cfg):
    return [c.strip() for c in re.split(r"[,;\s]+", str(cfg.get("telegram_chats") or "")) if c.strip()]

def _enviar_telegram(cfg, chat, texto):
    token = str(cfg.get("telegram_token") or "").strip()
    if not token:
        raise RuntimeError("Falta el token del bot de Telegram (lo da @BotFather).")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": texto, "disable_web_page_preview": False}, timeout=15)
    datos = r.json() if r.content else {}
    if not datos.get("ok"):
        desc = datos.get("description", f"HTTP {r.status_code}")
        if "chat not found" in desc.lower():
            desc = "chat no encontrado: escríbele primero al bot (o agrégalo al grupo) y usa 'Detectar chats'."
        elif r.status_code == 401:
            desc = "token del bot incorrecto."
        raise RuntimeError(f"Telegram: {desc}")
    return "entregado a Telegram"

def detectar_chats_telegram(token):
    """Chats que le escribieron al bot recientemente (para no tener que buscar el chat_id a mano)."""
    r = requests.get(f"https://api.telegram.org/bot{str(token).strip()}/getUpdates", timeout=15)
    datos = r.json()
    if not datos.get("ok"):
        raise RuntimeError(f"Telegram: {datos.get('description', 'token incorrecto')}")
    chats = {}
    for u in datos.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        ch = msg.get("chat") or {}
        if ch.get("id"):
            chats[str(ch["id"])] = ch.get("title") or " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x)
    return chats

def enviar_a_todos(alerta_id, texto, cfg=None):
    """Envía a todos los destinatarios activos y registra cada intento. Devuelve (enviados, errores)."""
    cfg = cfg or cargar_config()
    if cfg["sms_proveedor"] == "telegram":              # Telegram usa chats, no números de teléfono
        chats = _chats_telegram(cfg)
        if not chats:
            _registrar_sms(alerta_id, "-", texto, "telegram", "error", "Faltan los chats de Telegram (usa 'Detectar chats').")
            return 0, 1
        ok = err = 0
        for chat in chats:
            try:
                detalle, estado = _enviar_telegram(cfg, chat, texto), "enviado"
                ok += 1
            except Exception as e:
                detalle, estado = str(e), "error"
                err += 1
            _registrar_sms(alerta_id, f"telegram:{chat}", texto, "telegram", estado, detalle)
        return ok, err
    destinos = [d for d in listar_destinatarios() if d["activo"]]
    if not destinos:
        _registrar_sms(alerta_id, "-", texto, cfg["sms_proveedor"], "sin_destinatarios", "No hay destinatarios activos")
        return 0, 0
    ok = err = 0
    for d in destinos:
        try:
            if cfg["sms_proveedor"] == "twilio":
                detalle, estado = _enviar_twilio(cfg, d["telefono"], texto), "enviado"
            elif cfg["sms_proveedor"] == "webhook":
                detalle, estado = _enviar_webhook(cfg, d["telefono"], texto), "enviado"
            elif cfg["sms_proveedor"] == "android":
                detalle, estado = _enviar_android(cfg, d["telefono"], texto), "enviado"
            else:
                detalle, estado = "modo simulado: no se envió ningún SMS real", "simulado"
            ok += 1
        except Exception as e:
            detalle, estado = str(e), "error"
            err += 1
        _registrar_sms(alerta_id, d["telefono"], texto, cfg["sms_proveedor"], estado, detalle)
    return ok, err

_ULTIMO_SMS = {}
_SMS_LOCK = threading.Lock()

def notificar_alerta(alerta_id, audio, categoria, confianza, nivel):
    """Aplica nivel mínimo y enfriamiento por sensor y envía el SMS. Devuelve un texto para el registro."""
    cfg = cargar_config()
    if cfg["sms_nivel_minimo"] == "alta" and nivel != "alta":
        _registrar_sms(alerta_id, "-", "", cfg["sms_proveedor"], "omitido",
                       f"alerta de nivel {nivel}; el 'Nivel mínimo' configurado es alta")
        return "SMS omitido: nivel inferior al mínimo configurado"
    clave = (audio["sensor_id"], categoria)       # por sensor Y tipo: un disparo tras una motosierra sí se avisa
    # La espera evita decenas de mensajes por la MISMA motosierra que un sensor real reporta cada pocos segundos.
    # Los audios subidos a mano (pruebas) no vienen de un sensor en red: cada uno se avisa siempre.
    with conectar() as bd:
        es_sensor_red = bd.execute("SELECT 1 FROM sensor_config WHERE sensor_id=?", (audio["sensor_id"],)).fetchone() is not None
    with _SMS_LOCK:
        ahora = time.time()
        if es_sensor_red and ahora - _ULTIMO_SMS.get(clave, 0.0) < float(cfg["sms_enfriamiento_s"]):
            _registrar_sms(alerta_id, "-", "", cfg["sms_proveedor"], "omitido", f"Enfriamiento: ya se avisó {categoria} de este sensor hace poco")
            return f"SMS omitido por enfriamiento (ya se avisó {categoria} desde este sensor hace poco)"
        _ULTIMO_SMS[clave] = ahora
    ok, err = enviar_a_todos(alerta_id, texto_sms(audio, categoria, confianza, nivel), cfg)
    with conectar() as bd:
        bd.execute("UPDATE alertas SET sms_enviado=? WHERE id=?", (1 if ok else 0, alerta_id))
    return f"SMS ({cfg['sms_proveedor']}): {ok} enviado(s), {err} error(es)"

def estado_envio_alerta(alerta_id):
    """Resumen legible de lo que pasó con el aviso de una alerta (para el panel de alertas)."""
    with conectar() as bd:
        filas = bd.execute("SELECT proveedor, estado, detalle FROM sms_log WHERE alerta_id=? ORDER BY id", (alerta_id,)).fetchall()
    if not filas:
        return "aviso no enviado"
    prov = {"telegram": "Telegram", "twilio": "SMS Twilio", "android": "SMS Android", "webhook": "Webhook",
            "simulado": "SMS simulado"}.get(filas[0]["proveedor"], filas[0]["proveedor"])
    ok = sum(f["estado"] in ("enviado", "simulado") for f in filas)
    if ok:
        return f"{prov}: enviado a {ok} destino(s)"
    f = filas[-1]
    if f["estado"] == "omitido":
        return f"{prov}: omitido ({f['detalle']})"
    if f["estado"] == "sin_destinatarios":
        return f"{prov}: sin destinatarios"
    return f"{prov}: ERROR - {f['detalle']}"

def notificar_revision(alerta_id, audio, categoria):
    texto = _ascii(f"ODS15: la alerta del sensor {audio['sensor_id']} queda EN REVISION. "
                   f"El analisis con IA la clasifico como {categoria}; posible falsa alarma.")
    enviar_a_todos(alerta_id, texto)

def sms_de_prueba():
    """Envía un SMS de prueba a todos los destinatarios (ignora el enfriamiento). Devuelve (enviados, errores)."""
    return enviar_a_todos(0, "ODS15 prueba: el sistema de alertas por SMS funciona.")

# ------------------------------------------------------------------ sensores
def crear_sensor(sensor_id, nombre, lat, lon):
    """Registra un sensor y devuelve su token secreto."""
    sensor_id = str(sensor_id).strip()
    if not SENSOR_RE.match(sensor_id):
        raise ValueError("El identificador solo admite letras, números, guion y guion bajo (máximo 32).")
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Latitud o longitud fuera de rango.")
    inicializar()
    token = secrets.token_urlsafe(24)
    with conectar() as bd:
        bd.execute("INSERT INTO sensores(id,latitud,longitud,fecha_registro) VALUES(?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET latitud=excluded.latitud, longitud=excluded.longitud", (sensor_id, lat, lon, _ahora()))
        existente = bd.execute("SELECT token FROM sensor_config WHERE sensor_id=?", (sensor_id,)).fetchone()
        if existente:
            token = existente["token"]
            bd.execute("UPDATE sensor_config SET nombre=? WHERE sensor_id=?", (nombre or sensor_id, sensor_id))
        else:
            bd.execute("INSERT INTO sensor_config(sensor_id,nombre,token) VALUES(?,?,?)", (sensor_id, nombre or sensor_id, token))
    return token

def regenerar_token(sensor_id):
    token = secrets.token_urlsafe(24)
    with conectar() as bd:
        bd.execute("UPDATE sensor_config SET token=? WHERE sensor_id=?", (token, sensor_id))
    return token

def listar_sensores_red():
    inicializar()
    with conectar() as bd:
        return [dict(r) for r in bd.execute(
            "SELECT s.id, c.nombre, s.latitud, s.longitud, c.token, c.activo, c.ultimo_contacto, c.audios_recibidos "
            "FROM sensores s JOIN sensor_config c ON c.sensor_id=s.id ORDER BY s.id")]

def autenticar_sensor(sensor_id, token):
    if not SENSOR_RE.match(sensor_id or "") or not token:
        return None
    with conectar() as bd:
        fila = bd.execute("SELECT * FROM sensor_config WHERE sensor_id=?", (sensor_id,)).fetchone()
    if not fila or not hmac.compare_digest(str(fila["token"]), str(token)) or not fila["activo"]:
        return None
    return fila

# ------------------------------------------------------------------ celular como sensor
ARCHIVO_PAGINA_CELULAR = RAIZ / "celular_sensor.html"
# El certificado se guarda en una carpeta FIJA del usuario: si se descomprime una versión nueva en otra carpeta, se
# reutiliza el mismo y el celular no lo rechaza (con uno nuevo el navegador bloqueaba la conexión sin avisar).
DIR_CERT = Path(os.environ.get("APPDATA") or Path.home()) / "MonitoreoODS15" / "certificados"

def version_pagina_celular():
    try:
        return hashlib.sha1(ARCHIVO_PAGINA_CELULAR.read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"

def crear_codigo_emparejamiento(sensor_id, minutos=10):
    """Código de 6 dígitos de un solo uso para conectar un celular sin escribir el token largo."""
    with conectar() as bd:
        if not bd.execute("SELECT 1 FROM sensor_config WHERE sensor_id=?", (sensor_id,)).fetchone():
            raise ValueError(f"El sensor {sensor_id} no está registrado.")
        bd.execute("DELETE FROM emparejamientos WHERE expira<? OR sensor_id=?", (time.time(), sensor_id))
        codigo = f"{secrets.randbelow(10**6):06d}"
        bd.execute("INSERT OR REPLACE INTO emparejamientos(codigo,sensor_id,expira) VALUES(?,?,?)",
                   (codigo, sensor_id, time.time() + minutos * 60))
    return codigo

def canjear_codigo(codigo):
    codigo = re.sub(r"\D", "", str(codigo or ""))
    with conectar() as bd:
        fila = bd.execute("SELECT * FROM emparejamientos WHERE codigo=? AND expira>=?", (codigo, time.time())).fetchone()
        if not fila:
            return None
        bd.execute("DELETE FROM emparejamientos WHERE codigo=?", (codigo,))
        s = bd.execute("SELECT c.sensor_id, c.nombre, c.token FROM sensor_config c WHERE c.sensor_id=?", (fila["sensor_id"],)).fetchone()
    return dict(s) if s else None

def certificado_https():
    """Certificado propio (autofirmado) para servir HTTPS en la red local. Se crea una vez en 'certificados/'.
    El celular mostrará un aviso de 'conexión no privada' la primera vez: es normal, porque no lo firmó una autoridad."""
    cert, clave = DIR_CERT / "servidor.pem", DIR_CERT / "servidor.key"
    if cert.exists() and clave.exists():
        return cert, clave
    viejo = RAIZ / "certificados"                      # versiones anteriores lo guardaban junto al programa
    if (viejo / "servidor.pem").exists() and (viejo / "servidor.key").exists():
        try:
            DIR_CERT.mkdir(parents=True, exist_ok=True)
            shutil.copy2(viejo / "servidor.pem", cert); shutil.copy2(viejo / "servidor.key", clave)
            return cert, clave
        except OSError:
            return viejo / "servidor.pem", viejo / "servidor.key"
    try:
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise RuntimeError("Falta la librería 'cryptography' para HTTPS. Instálala con: pip install cryptography")
    DIR_CERT.mkdir(parents=True, exist_ok=True)
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Monitoreo ODS15")])
    ips = {"127.0.0.1", *ips_locales()}
    alt = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    ahora = datetime.datetime.now(datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(ahora - datetime.timedelta(days=1))
         .not_valid_after(ahora + datetime.timedelta(days=825))
         .add_extension(x509.SubjectAlternativeName(alt), critical=False).sign(k, hashes.SHA256()))
    clave.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return cert, clave

def normalizar_volumen_wav(ruta, pico_objetivo_db=-3.0, max_ganancia_db=30.0):
    """Sube el volumen de un WAV de sensor hasta que su pico quede en -3 dBFS (máx. +30 dB). Los celulares graban
    sin ganancia automática (para no deformar los disparos) y el audio guardado se oía muy bajito."""
    try:
        sr, x = wavfile.read(ruta)
    except Exception:
        return 0.0
    if x.dtype != np.int16 or x.size == 0:
        return 0.0
    pico = float(np.max(np.abs(x.astype(np.int32)))) / 32768.0
    if pico <= 0:
        return 0.0
    g_db = float(np.clip(pico_objetivo_db - 20 * np.log10(pico), 0.0, max_ganancia_db))
    if g_db < 1.0:
        return 0.0
    y = np.clip(np.round(x.astype(np.float32) * 10 ** (g_db / 20)), -32768, 32767).astype(np.int16)
    tmp = Path(str(ruta) + ".tmp")
    wavfile.write(tmp, sr, y)
    os.replace(tmp, ruta)
    return round(g_db, 1)

def actualizar_ubicacion(sensor_id, lat, lon):
    """El celular envía su GPS: la alerta mostrará la ubicación real del 'sensor'."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return
    if -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0):
        with conectar() as bd:
            bd.execute("UPDATE sensores SET latitud=?, longitud=? WHERE id=?", (lat, lon, sensor_id))

def ips_locales():
    """Todas las IPv4 del computador en la red local, la más probable primero. Antes se mostraba solo una, y en
    equipos con VPN, VirtualBox, WSL o Hyper-V a veces era la de un adaptador virtual que el celular no alcanza."""
    candidatas = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))                 # no envía nada: solo pregunta qué interfaz sale a la red
        candidatas.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        candidatas += socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        pass
    def puntaje(ip):
        if ip.startswith(("192.168.43.", "172.20.10.")):
            return 0                                # punto de acceso de Android / iPhone
        if ip.startswith(("192.168.56.", "192.168.99.")) or re.match(r"172\.(1[7-9]|2\d|3[01])\.", ip):
            return 3                                # VirtualBox, Docker, WSL, Hyper-V
        if ip.startswith(("192.168.", "10.")):
            return 1
        return 2
    vistas = []
    for ip in candidatas:
        if ip not in vistas and not ip.startswith(("127.", "169.254.", "0.")):
            vistas.append(ip)
    primera = vistas[:1]
    resto = sorted(vistas[1:], key=puntaje)
    return [ip for ip in primera + resto if puntaje(ip) < 3] + [ip for ip in primera + resto if puntaje(ip) == 3] or ["127.0.0.1"]

def ip_local():
    return ips_locales()[0]

REGLA_FIREWALL = "MonitoreoODS15"

def firewall_permitido():
    """True/False si existe la regla del firewall de Windows; None si no es Windows o no se puede saber."""
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={REGLA_FIREWALL}"],
                           capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return None

def permitir_firewall(puertos):
    """Crea (como administrador; Windows pide confirmación) una regla que deja entrar a los sensores y celulares por
    esos puertos en cualquier tipo de red. Sin ella, si Windows marca el Wi-Fi como 'Público', el celular no conecta."""
    if os.name != "nt":
        raise RuntimeError("Solo es necesario en Windows.")
    lista = ",".join(str(int(p)) for p in puertos if p)
    orden = (f"/c netsh advfirewall firewall delete rule name={REGLA_FIREWALL} & netsh advfirewall firewall add rule "
             f"name={REGLA_FIREWALL} dir=in action=allow protocol=TCP localport={lista} profile=any")
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Start-Process -FilePath cmd.exe -ArgumentList '{orden}' -Verb RunAs -Wait -WindowStyle Hidden"],
                       capture_output=True, text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not firewall_permitido():
        raise RuntimeError("No se creó la regla (¿se canceló el permiso de administrador?).")

class ServidorSensores:
    """Recibe audio por HTTP (POST /audio con X-Sensor-Id y X-Sensor-Token), lo guarda en la base de datos
    y lo analiza en hilos propios: la alerta (y el SMS) salen en cuanto termina la etapa acústica."""
    def __init__(self, callback_log=None, callback_evento=None, usar_ia=lambda: True, hilos=2):
        self.log = callback_log or print
        self.evento = callback_evento or (lambda: None)
        self.usar_ia = usar_ia
        self.n_hilos = max(1, int(hilos))
        self.cola = Queue()
        self.httpd = None
        self.hilos = []
        self._parar = threading.Event()
        self.puerto = None
        self.red = False

    @property
    def activo(self):
        return self.httpd is not None

    def encolar(self, audio_id):
        self.cola.put(audio_id)

    def aviso_pausa(self, sensor_id, motivo):
        """Avisa en el registro como máximo una vez por minuto (los sensores siguen enviando cada pocos segundos)."""
        self.ignorados = getattr(self, "ignorados", 0) + 1
        ahora = time.time()
        if ahora - getattr(self, "_ultimo_aviso", 0.0) >= 60:
            self._ultimo_aviso = ahora
            self.log(f"⏸ [{_hora_ms()}] Audio de {sensor_id} recibido pero NO analizado: {motivo} "
                     f"({self.ignorados} audio(s) sin analizar desde la pausa).")

    def iniciar(self, puerto=8000, red=False, puerto_https=None):
        if self.activo:
            return
        if self.log not in [o[0] for o in COLA_IA.oyentes]:
            COLA_IA.registrar(self.log, self.evento)
        recuperar_interrumpidos()
        red_neuronal()                                          # se carga antes de recibir el primer audio
        servidor = self
        class Manejador(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a):
                pass
            def _json(self, codigo, datos):
                cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
                self.send_response(codigo)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(cuerpo)))
                if codigo >= 400:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(cuerpo)
            def _sensor(self):
                return autenticar_sensor(self.headers.get("X-Sensor-Id", "").strip(), self.headers.get("X-Sensor-Token", "").strip())
            def _cuerpo(self, codigo, contenido, tipo):
                self.send_response(codigo)
                self.send_header("Content-Type", tipo)
                self.send_header("Content-Length", str(len(contenido)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(contenido)
            def _emparejar(self):
                ip = self.client_address[0]
                fallos = servidor.fallos_emparejar.get(ip, (0, 0.0))
                if fallos[0] >= 8 and time.time() - fallos[1] < 600:
                    return self._json(429, {"error": "Demasiados intentos. Espera 10 minutos."})
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    datos = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                except ValueError:
                    datos = {}
                s = canjear_codigo(datos.get("codigo"))
                if not s:
                    servidor.fallos_emparejar[ip] = (fallos[0] + 1, time.time())
                    return self._json(403, {"error": "Código incorrecto o vencido. Genera uno nuevo en el programa."})
                servidor.fallos_emparejar.pop(ip, None)
                servidor.log(f"📱 [{_hora_ms()}] Celular conectado como sensor {s['sensor_id']} ({ip}).")
                servidor.evento()
                return self._json(200, s)
            def do_GET(self):
                ruta, _, consulta = self.path.partition("?")
                if ruta in ("/", "/celular"):
                    if not isinstance(self.connection, ssl.SSLSocket) and servidor.puerto_https:
                        # el micrófono del navegador solo funciona con HTTPS: se redirige al puerto seguro
                        host = (self.headers.get("Host") or "localhost").split(":")[0]
                        self.send_response(302)
                        self.send_header("Location", f"https://{host}:{servidor.puerto_https}/celular")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    try:
                        pagina = ARCHIVO_PAGINA_CELULAR.read_bytes().replace(b"__VERSION_PAGINA__", version_pagina_celular().encode())
                        return self._cuerpo(200, pagina, "text/html; charset=utf-8")
                    except OSError:
                        return self._json(404, {"error": "Falta celular_sensor.html"})
                if ruta == "/resultado":
                    s = self._sensor()
                    if not s:
                        return self._json(401, {"error": "Sensor o token inválido"})
                    m_id = re.search(r"audio_id=(\d+)", consulta)
                    if not m_id:
                        return self._json(400, {"error": "Falta audio_id"})
                    with conectar() as bd:
                        f = bd.execute(
                            "SELECT au.estado, an.categoria, an.confianza, an.nivel_alerta, an.justificacion, "
                            "(SELECT COUNT(*) FROM alertas al WHERE al.analisis_id=an.id) AS n_alertas "
                            "FROM audios au LEFT JOIN analisis an ON an.audio_id=au.id "
                            "WHERE au.id=? AND au.sensor_id=? ORDER BY an.id DESC LIMIT 1",
                            (int(m_id.group(1)), s["sensor_id"])).fetchone()
                    if not f:
                        return self._json(404, {"error": "Audio no encontrado"})
                    d = dict(f)
                    d["alerta"] = bool(d.pop("n_alertas")) and d["categoria"] in CATEGORIAS_AMENAZA
                    d["justificacion"] = (d.get("justificacion") or "")[:200]
                    return self._json(200, d)
                if ruta == "/config":
                    s = self._sensor()
                    if not s:
                        return self._json(401, {"error": "Sensor o token inválido"})
                    return self._json(200, config_sensores())
                if ruta == "/ping":
                    s = self._sensor()
                    if not s:
                        return self._json(401, {"error": "Sensor o token inválido"})
                    with conectar() as bd:
                        bd.execute("UPDATE sensor_config SET ultimo_contacto=? WHERE sensor_id=?", (time.time(), s["sensor_id"]))
                    return self._json(200, {"ok": True, "hora": time.time()})
                self._json(200, {"servicio": "Monitoreo acustico ODS 15", "enviar_audio": "POST /audio"})
            def do_POST(self):
                try:
                    if self.path.split("?")[0] == "/emparejar":
                        return self._emparejar()
                    if self.path.split("?")[0] != "/audio":
                        return self._json(404, {"error": "Ruta no encontrada"})
                    s = self._sensor()
                    if not s:
                        return self._json(401, {"error": "Sensor o token inválido"})
                    n = int(self.headers.get("Content-Length") or 0)
                    if n <= 0:
                        return self._json(400, {"error": "Falta el audio en el cuerpo de la petición"})
                    if n > MAX_BYTES_SENSOR:
                        return self._json(413, {"error": f"Audio demasiado grande (máximo {MAX_BYTES_SENSOR // 1048576} MB)"})
                    datos = self.rfile.read(n)
                    nombre = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(self.headers.get("X-Filename", "audio.wav")))[:60] or "audio.wav"
                    if Path(nombre).suffix.lower() not in EXTENSIONES_AUDIO:
                        return self._json(415, {"error": "Formato no soportado"})
                    sid = s["sensor_id"]
                    if self.headers.get("X-Lat") and self.headers.get("X-Lon"):
                        actualizar_ubicacion(sid, self.headers.get("X-Lat"), self.headers.get("X-Lon"))
                    permitido, motivo = estado_analisis()
                    if not permitido:      # pausa o fuera de horario: no se guarda ni se analiza (ahorra IA y disco)
                        with conectar() as bd:
                            bd.execute("UPDATE sensor_config SET ultimo_contacto=? WHERE sensor_id=?", (time.time(), sid))
                        servidor.aviso_pausa(sid, motivo)
                        return self._json(200, {"ok": True, "analizado": False, "motivo": motivo, "config": config_sensores()})
                    servidor.ignorados = 0
                    carpeta = DIR_AUDIOS / sid
                    carpeta.mkdir(parents=True, exist_ok=True)
                    destino = carpeta / f"{int(time.time() * 1000)}_{nombre}"
                    parcial = destino.with_name(destino.name + ".part")
                    parcial.write_bytes(datos)
                    os.replace(parcial, destino)
                    ganancia = normalizar_volumen_wav(destino) if destino.suffix.lower() == ".wav" else 0.0
                    audio_id = registrar_audio(destino, sid, estado="en_cola")
                    if audio_id is None:
                        destino.unlink(missing_ok=True)
                        return self._json(200, {"ok": True, "duplicado": True, "mensaje": "Audio ya recibido antes"})
                    with conectar() as bd:
                        bd.execute("UPDATE sensor_config SET ultimo_contacto=?, audios_recibidos=audios_recibidos+1 WHERE sensor_id=?",
                                   (time.time(), sid))
                    servidor.encolar(audio_id)
                    omitidos = int(self.headers.get("X-Omitidos") or 0)
                    if omitidos:
                        with conectar() as bd:
                            bd.execute("UPDATE sensor_config SET omitidos_silencio=COALESCE(omitidos_silencio,0)+? WHERE sensor_id=?",
                                       (omitidos, sid))
                    servidor.log(f"📡 [{_hora_ms()}] Sensor {sid}: audio recibido"
                                 f"{f' tras {omitidos} trozo(s) de silencio no enviados' if omitidos else ''} ({len(datos) // 1024} KB"
                                 f"{f', volumen +{ganancia:.0f} dB' if ganancia else ''}) -> en análisis")
                    servidor.evento()
                    self._json(202, {"ok": True, "audio_id": audio_id, "en_cola": servidor.cola.qsize(), "config": config_sensores()})
                except Exception as e:
                    servidor.log(f"✗ Error recibiendo audio de un sensor: {e}")
                    try:
                        self._json(500, {"error": "Error interno"})
                    except Exception:
                        pass
        class Http(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            def handle_error(self, request, client_address):
                pass        # p. ej. el celular cierra la conexión al ver el aviso del certificado: no es un error
        self.httpd = Http(("0.0.0.0" if red else "127.0.0.1", int(puerto)), Manejador)
        self.puerto, self.red = int(puerto), bool(red)
        self.fallos_emparejar = {}
        self.httpsd, self.puerto_https, self.error_https = None, None, None
        if red:
            p_https = int(puerto_https or cargar_config().get("servidor_puerto_https", 8443))
            try:
                cert, clave = certificado_https()
                contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                contexto.load_cert_chain(cert, clave)
                self.httpsd = Http(("0.0.0.0", p_https), Manejador)
                # el saludo TLS se hace en el hilo de cada conexión, no en el que acepta conexiones
                self.httpsd.socket = contexto.wrap_socket(self.httpsd.socket, server_side=True, do_handshake_on_connect=False)
                self.puerto_https = p_https
            except Exception as e:
                self.error_https = str(e)
        self._parar.clear()
        with conectar() as bd:
            pendientes = [r["id"] for r in bd.execute("SELECT id FROM audios WHERE estado='en_cola' ORDER BY id")]
        for i in pendientes:
            self.encolar(i)
        self.hilos = [threading.Thread(target=self._trabajar, name=f"Sensor-Worker_{i}", daemon=True) for i in range(self.n_hilos)]
        self.hilos.append(threading.Thread(target=self.httpd.serve_forever, name="Sensor-HTTP", daemon=True))
        if self.httpsd:
            self.hilos.append(threading.Thread(target=self.httpsd.serve_forever, name="Sensor-HTTPS", daemon=True))
        for h in self.hilos:
            h.start()
        host = ip_local() if red else "localhost"
        self.log(f"📡 Servidor de sensores activo en http://{host}:{self.puerto}/audio ({'red local' if red else 'solo este equipo'})")
        if self.httpsd:
            self.log(f"📱 Celulares: abrir https://{host}:{self.puerto_https}/celular")
        elif red and self.error_https:
            self.log(f"⚠ Sin HTTPS para celulares: {self.error_https}")

    def detener(self):
        if not self.activo:
            return
        self._parar.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        if getattr(self, "httpsd", None):
            self.httpsd.shutdown()
            self.httpsd.server_close()
            self.httpsd = None
        self.log("📡 Servidor de sensores detenido.")

    def _trabajar(self):
        while not self._parar.is_set():
            try:
                audio_id = self.cola.get(timeout=0.5)
            except Empty:
                continue
            try:
                with conectar() as bd:
                    fila = bd.execute("SELECT a.*,s.latitud,s.longitud FROM audios a JOIN sensores s ON s.id=a.sensor_id WHERE a.id=?", (audio_id,)).fetchone()
                if not fila:
                    continue
                res, mensaje = analizar_audio(dict(fila), usar_ia=self.usar_ia(), callback_log=self.log)
                alerta_txt = f" | Alerta+SMS: {res['t_alerta_s']:.3f}s" if res.get("t_alerta_s") is not None else ""
                self.log(f"✓ [{fila['sensor_id']}] '{fila['nombre_archivo']}' -> {res['categoria'].upper()} ({res['confianza']:.0%}) | "
                         f"Acústica: {res['t_audio_s']:.3f}s | IA: {res['t_ia_s']:.3f}s{alerta_txt}")
                if mensaje:
                    self.log(f"  🚨 {mensaje}")
            except YaTomado:
                pass
            except Exception as e:
                self.log(f"✗ Error analizando audio #{audio_id}: {e}")
            finally:
                self.evento()

PALABRAS_ETIQUETA = {
    "motosierra": "motosierra",
    "chainsaw": "motosierra",
    "tala": "motosierra",
    "sierra": "motosierra",
    "disparo": "disparo",
    "disparos": "disparo",
    "gunshot": "disparo",
    "gun": "disparo",
    "shot": "disparo",
    "tiro": "disparo",
    "rifle": "disparo",
    "escopeta": "disparo",
    "incendio": "incendio",
    "fuego": "incendio",
    "fire": "incendio",
    "crackling": "incendio",
    "motor": "motor_vehiculo",
    "vehiculo": "motor_vehiculo",
    "vehicle": "motor_vehiculo",
    "camion": "motor_vehiculo",
    "carro": "motor_vehiculo",
    "tractor": "motor_vehiculo",
    "engine": "motor_vehiculo",
    "truck": "motor_vehiculo",
    "ambiente": "ambiente_natural",
    "bosque": "ambiente_natural",
    "lluvia": "ambiente_natural",
    "rain": "ambiente_natural",
    "bird": "ambiente_natural",
    "birds": "ambiente_natural",
    "aves": "ambiente_natural",
    "viento": "ambiente_natural",
    "wind": "ambiente_natural",
    "rio": "ambiente_natural",
    "agua": "ambiente_natural",
    "water": "ambiente_natural",
    "winter": "ambiente_natural",
    "nature": "ambiente_natural",
    "naturaleza": "ambiente_natural",
    "ambience": "ambiente_natural",
    "ambient": "ambiente_natural",
    "morning": "ambiente_natural",
    "forest": "ambiente_natural"
}

def _esperada(nombre):
    nombre = Path(nombre).stem.lower()
    for palabra, categoria in PALABRAS_ETIQUETA.items():
        if re.search(rf"(?<![a-záéíóúñ]){re.escape(palabra)}(?![a-záéíóúñ])", nombre):
            return categoria
    return None

DIR_ENTRENAMIENTO = RAIZ / "entrenamiento"

def etiquetar_audio(audio_id, categoria):
    """Guarda la categoría REAL de un audio (la dice una persona): alimenta el KPI de precisión y copia el audio a
    entrenamiento/<categoría>/ para entrenar el modelo propio con 'Entrenar con mis audios'."""
    if categoria not in CATEGORIAS:
        raise ValueError(f"Categoría desconocida: {categoria}")
    with conectar() as bd:
        f = bd.execute("SELECT ruta, nombre_archivo, etiqueta FROM audios WHERE id=?", (audio_id,)).fetchone()
        if not f:
            raise ValueError("Audio no encontrado")
        bd.execute("UPDATE audios SET etiqueta=? WHERE id=?", (categoria, audio_id))
    for c in CATEGORIAS:                                   # si antes se marcó con otra categoría, se mueve
        viejo = DIR_ENTRENAMIENTO / c / f"{audio_id}_{f['nombre_archivo']}"
        if viejo.exists() and c != categoria:
            viejo.unlink()
    destino = DIR_ENTRENAMIENTO / categoria / f"{audio_id}_{f['nombre_archivo']}"
    destino.parent.mkdir(parents=True, exist_ok=True)
    if Path(f["ruta"]).exists():
        shutil.copy2(f["ruta"], destino)
    return destino

def calcular_kpis_actuales():
    inicializar()
    with conectar() as con:
        analisis = con.execute(
            "SELECT an.*, au.nombre_archivo, au.etiqueta FROM analisis an JOIN audios au ON au.id = an.audio_id"
        ).fetchall()
        alertas = con.execute("SELECT * FROM alertas").fetchall()

    if not analisis:
        return {
            "total_analisis": 0,
            "total_alertas": 0,
            "prom_deteccion_s": None,
            "prom_acustico_s": None,
            "prom_ia_s": None,
            "cumple_deteccion": None,
            "precision": None,
            "aciertos": 0,
            "total_etiquetados": 0,
            "cumple_precision": None,
            "tasa_falsas": None,
            "falsas": 0,
            "total_inocuos": 0,
            "cumple_falsas": None,
            "prom_alerta_s": None,
            "cumple_alerta": None
        }

    prom_acustico = sum((a["t_audio_s"] or 0.0) for a in analisis) / len(analisis)
    prom_ia = sum((a["t_ia_s"] or 0.0) for a in analisis) / len(analisis)
    prom_det = prom_acustico
    cumple_det = prom_det <= KPI_TIEMPO_DETECCION_S

    # El KPI mide el aviso automático de la etapa acústica (< 1 s). Las alertas que solo aparecen después de la
    # revisión de la IA (que tarda lo que tarde LM Studio) se cuentan, pero no se promedian en este tiempo.
    alertas = [a for a in alertas if (a["origen"] or "acustica") != "ia"] or alertas
    if alertas:
        prom_al = sum((a["t_alerta_s"] or 0.0) for a in alertas) / len(alertas)
        cumple_al = prom_al <= KPI_TIEMPO_ALERTA_S
    else:
        prom_al = 0.0
        cumple_al = True

    etiquetados = [(a, a["etiqueta"] or _esperada(a["nombre_archivo"])) for a in analisis]
    etiquetados = [(a, e) for a, e in etiquetados if e]

    if etiquetados:
        aciertos = sum(a["categoria"] == e for a, e in etiquetados)
        precision = aciertos / len(etiquetados)
        cumple_prec = precision >= KPI_PRECISION_MIN

        inocuos = [(a, e) for a, e in etiquetados if e not in CATEGORIAS_AMENAZA]
        if inocuos:
            falsas = sum(
                a["categoria"] in CATEGORIAS_AMENAZA and (a["confianza"] or 0.0) >= UMBRAL_CONFIANZA_ALERTA
                for a, _ in inocuos
            )
            tasa_falsas = falsas / len(inocuos)
            cumple_falsas = tasa_falsas <= KPI_FALSAS_ALARMAS_MAX
        else:
            falsas = 0
            tasa_falsas = 0.0
            cumple_falsas = True
    else:
        aciertos = 0
        precision = None
        cumple_prec = None
        inocuos = []
        falsas = 0
        tasa_falsas = None
        cumple_falsas = None

    return {
        "total_analisis": len(analisis),
        "total_alertas": len(alertas),
        "prom_deteccion_s": prom_det,
        "prom_acustico_s": prom_acustico,
        "prom_ia_s": prom_ia,
        "cumple_deteccion": cumple_det,
        "precision": precision,
        "aciertos": aciertos,
        "total_etiquetados": len(etiquetados),
        "cumple_precision": cumple_prec,
        "tasa_falsas": tasa_falsas,
        "falsas": falsas,
        "total_inocuos": len(inocuos),
        "cumple_falsas": cumple_falsas,
        "prom_alerta_s": prom_al,
        "cumple_alerta": cumple_al
    }

def generar_audios_prueba():
    inicializar()
    sr = FRECUENCIA_MUESTREO
    dur = 3.0
    n = int(sr * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    rng = np.random.default_rng(42)

    disparo = 0.0015 * rng.standard_normal(n)
    i0 = int(0.85 * sr)
    largo_imp = int(0.065 * sr)
    env = np.exp(-np.linspace(0.0, 5.5, largo_imp))
    disparo[i0:i0 + largo_imp] += 0.95 * rng.standard_normal(largo_imp) * env

    f0_sierra = 180.0
    sierra = np.zeros(n)
    for k, amp in [(1, 0.40), (2, 0.25), (3, 0.15), (6, 0.22), (8, 0.28), (10, 0.25), (12, 0.18), (14, 0.12)]:
        sierra += amp * np.sin(2.0 * np.pi * (f0_sierra * k) * t)
    sierra *= (0.88 + 0.12 * np.sin(2.0 * np.pi * 6.0 * t))
    sierra += 0.03 * rng.standard_normal(n)

    f0_motor = 95.0
    motor = (
        0.55 * np.sin(2.0 * np.pi * f0_motor * t)
        + 0.30 * np.sin(2.0 * np.pi * (2 * f0_motor) * t)
        + 0.15 * np.sin(2.0 * np.pi * (3 * f0_motor) * t)
        + 0.02 * rng.standard_normal(n)
    )

    ambiente = 0.08 * rng.standard_normal(n) * (0.85 + 0.15 * np.sin(2.0 * np.pi * 0.4 * t))

    archivos = [
        ("disparo_prueba_1.wav", disparo),
        ("motosierra_prueba_1.wav", sierra),
        ("motor_vehiculo_prueba_1.wav", motor),
        ("ambiente_bosque_prueba_1.wav", ambiente)
    ]

    creados = 0
    for nombre, senal in archivos:
        senal = senal / (np.max(np.abs(senal)) + EPS) * 0.85
        pcm = np.clip(senal * 32767.0, -32768, 32767).astype(np.int16)
        ruta = DIR_AUDIOS / nombre
        wavfile.write(ruta, sr, pcm)
        if registrar_audio(ruta, SENSOR_POR_DEFECTO) is not None:
            creados += 1
    return creados

def cmd_iniciar(_):
    inicializar()
    print(f"Base de datos lista en {DIR_BD}")
    print(f"Copia los audios de prueba en: {DIR_AUDIOS}")

def cmd_verificar(_):
    d = diagnosticar_ia()
    print(f"URL: {d['url']} | servidor: {'sí' if d['servidor'] else 'no'} | modelo: {d['modelo']} | cargados: {d['cargados']}")
    print(("OK: " if d["responde"] else "x ") + d["mensaje"] + (f" ({d['t_respuesta_s']} s)" if d["t_respuesta_s"] else ""))
    print(f"Red neuronal de audio: {d['red_neuronal']}")
def cmd_servidor(a):
    inicializar()
    srv = ServidorSensores(callback_log=print, usar_ia=lambda: not a.sin_ia)
    srv.iniciar(a.puerto, a.red)
    print("Esperando audio de los sensores... (Ctrl+C para detener)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.detener()
def cmd_sensor(a):
    token = crear_sensor(a.id, a.nombre or a.id, a.lat, a.lon)
    print(f"Sensor {a.id} registrado.\nToken: {token}")
def cmd_destinatario(a):
    print("Destinatario guardado:", agregar_destinatario(a.nombre, a.telefono))
def cmd_sms_prueba(_):
    ok, err = sms_de_prueba()
    print(f"SMS de prueba: {ok} enviado(s), {err} error(es)")

def cmd_celular(a):
    inicializar()
    if a.id not in {x["id"] for x in listar_sensores_red()}:
        crear_sensor(a.id, f"Celular {a.id}", a.lat, a.lon)
    codigo = crear_codigo_emparejamiento(a.id)
    puerto = cargar_config().get("servidor_puerto_https", 8443)
    print(f"Sensor {a.id} listo. Con el servidor en marcha (python monitoreo.py servidor --red):")
    ips = ips_locales()
    print(f"  1. En el celular abre  https://{ips[0]}:{puerto}/celular  (acepta el aviso del certificado)")
    for ip in ips[1:]:
        print(f"     si no abre, prueba  https://{ip}:{puerto}/celular")
    if firewall_permitido() is False:
        print("  ! El firewall de Windows puede bloquear al celular. Ejecuta: python monitoreo.py firewall")
    print(f"  2. Escribe el código   {codigo}   (vence en 10 minutos)")

def cmd_firewall(_):
    cfg = cargar_config()
    try:
        permitir_firewall([cfg.get("servidor_puerto", 8000), cfg.get("servidor_puerto_https", 8443)])
        print("Listo: el firewall de Windows ya permite la conexión de sensores y celulares.")
    except Exception as e:
        sys.exit(str(e))

def cmd_admin(a):
    inicializar()
    try:
        if a.accion == "pausar":
            administrar(a.pin, analisis_activo=False)
        elif a.accion == "activar":
            administrar(a.pin, analisis_activo=True)
        elif a.accion == "ia-on":
            administrar(a.pin, ia_activa=True)
        elif a.accion == "ia-off":
            administrar(a.pin, ia_activa=False)
        elif a.accion == "horario":
            if len(a.valores) != 2:
                sys.exit("Uso: python monitoreo.py admin horario 18:00 06:00 [--pin 1234]")
            administrar(a.pin, horario_activo=True, horario_inicio=a.valores[0], horario_fin=a.valores[1])
        elif a.accion == "sonido-on":
            administrar(a.pin, solo_con_sonido=True,
                        **({"sensibilidad_sonido": a.valores[0]} if a.valores else {}))
        elif a.accion == "sonido-off":
            administrar(a.pin, solo_con_sonido=False)
        elif a.accion == "sin-horario":
            administrar(a.pin, horario_activo=False)
        elif a.accion == "pin":
            cambiar_pin(a.pin, a.valores[0] if a.valores else "")
            print("PIN actualizado." if a.valores else "PIN eliminado.")
    except (PermissionError, ValueError) as e:
        sys.exit(str(e))
    cfg = cargar_config()
    permitido, motivo = estado_analisis(cfg)
    print(f"Análisis: {'ACTIVO' if permitido else 'DETENIDO'} ({motivo})")
    print(f"IA (LM Studio): {'activada' if cfg['ia_activa'] else 'desactivada'}"
          f"{' - en todas las señales' if cfg.get('ia_todas') else ' - solo en casos dudosos'}")
    print(f"Horario: {cfg['horario_inicio']}-{cfg['horario_fin']}" if cfg["horario_activo"] else "Horario: 24 horas")
    print("Sensores: " + (f"envían solo cuando hay sonido (sensibilidad {cfg.get('sensibilidad_sonido', 'media')})"
                          if cfg.get("solo_con_sonido") else "envío continuo"))
    print(f"PIN de administrador: {'configurado' if cfg.get('admin_pin') else 'sin PIN'}")
    print(f"Consultas a la IA hoy: {consultas_ia_hoy()}")

def cmd_subir(a):
    audio_id = subir_audio(Path(a.audio), a.sensor)
    if audio_id is None:
        print("Ese audio ya estaba registrado por contenido idéntico. No se duplicó.")
    else:
        print(f"Audio registrado: id {audio_id}.")

def cmd_analizar(a):
    inicializar()
    procesar_pendientes(not a.sin_ia)

def cmd_vigilar(a):
    inicializar()
    print(f"Vigilando {DIR_AUDIOS} cada {a.intervalo} s (Ctrl+C para salir)...")
    try:
        while True:
            permitido, motivo = estado_analisis()
            if not permitido:
                time.sleep(max(5.0, a.intervalo))
                continue
            if escanear_entrada():
                procesar_pendientes(not a.sin_ia)
            time.sleep(max(0.2, a.intervalo))
    except KeyboardInterrupt:
        print("\nVigilancia detenida.")

def cmd_estado(_):
    inicializar()
    filas = resumen_resultados()
    print(f"Audios: {contar('audios')} | Análisis: {contar('analisis')} | Alertas: {contar('alertas')}\n")
    for f in filas:
        resultado = f"{f['categoria']} ({f['confianza']:.0%}, {f['fuente']})" if f["categoria"] else "sin analizar"
        print(f"#{f['audio_id']:<3} {f['nombre_archivo']:<45} {f['estado']:<10} {resultado}")
    for alerta in alertas_activas():
        print(f"\n[ALERTA #{alerta['id']}] {alerta['mensaje']}")

def cmd_ruta(_):
    inicializar()
    mostrar_ruta_patrullaje()

def cmd_reporte(_):
    k = calcular_kpis_actuales()
    if k["total_analisis"] == 0:
        print("Aún no hay análisis registrados.")
        return

    def estado(x):
        return "CUMPLE" if x else "NO CUMPLE"

    print(f"Audios analizados: {k['total_analisis']} | Alertas emitidas: {k['total_alertas']}\n")
    print(f"KPI Tiempo de detección <= {KPI_TIEMPO_DETECCION_S:.0f}s: {k['prom_deteccion_s']:.3f}s - {estado(k['cumple_deteccion'])}")
    print(f"KPI Tiempo de envío de alerta <= {KPI_TIEMPO_ALERTA_S:.0f}s: {k['prom_alerta_s']:.3f}s - {estado(k['cumple_alerta'])}")
    if k["precision"] is not None:
        print(f"KPI Precisión >= {KPI_PRECISION_MIN:.0%}: {k['precision']:.1%} - {estado(k['cumple_precision'])} ({k['aciertos']}/{k['total_etiquetados']})")
    if k["tasa_falsas"] is not None:
        print(f"KPI Falsas alarmas <= {KPI_FALSAS_ALARMAS_MAX:.0%}: {k['tasa_falsas']:.1%} - {estado(k['cumple_falsas'])} ({k['falsas']}/{k['total_inocuos']})")

def cmd_limpiar(a):
    if not a.si and input("Esto borra TODOS los audios y resultados. ¿Continuar? (s/N): ").lower() != "s":
        print("Cancelado.")
        return
    borrados, fallidos = limpiar_todo()
    print(f"Base de datos vacía de nuevo ({borrados} audios borrados"
          + (f"; {len(fallidos)} en uso no se pudieron borrar del disco, pero ya no aparecerán" if fallidos else "") + ").")


class InterfazMonitoreo:
    def __init__(self, root):
        self.root = root
        self.root.title("Sistema de Monitoreo Acústico e Inteligencia Artificial — Panel de Control y KPIs")
        self.root.geometry("1380x860")
        self.root.minsize(1180, 740)
        self.root.configure(bg="#0f172a")

        self.usar_ia_var = tk.BooleanVar(value=True)
        _aplicar_modo_ia()
        self.ia_todas_var = tk.BooleanVar(value=IA_MODO == "siempre")
        self.ventana_admin = None
        self.workers_var = tk.IntVar(value=4)
        self.ventana_sensores = None
        self._refresco_pendiente = False
        self.servidor = ServidorSensores(callback_log=self.log, callback_evento=self._refresco_diferido,
                                         usar_ia=lambda: self.usar_ia_var.get())
        self.vigilando = False
        self.procesando = False
        self.info_hilos = {}
        self.ultimo_lote = {"procesados": 0, "t_paralelo_s": 0.0, "t_secuencial_s": 0.0, "workers": 4}

        COLA_IA.registrar(self.log, self._refresco_diferido)
        recuperar_interrumpidos()
        threading.Thread(target=red_neuronal, daemon=True).start()   # carga la red neuronal en segundo plano (~0,3 s)
        self._configurar_estilos()
        self._construir_ui()
        self.actualizar_todo()
        self._verificar_ia_silencioso()
        self._refrescar_estado_admin()
        self._tick_cola()
        self.root.after(800, self._encender_servidor_guardado)
        self.root.protocol("WM_DELETE_WINDOW", self._cerrar)

    def _configurar_estilos(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Treeview", background="#1e293b", foreground="#f8fafc", fieldbackground="#1e293b", rowheight=30, font=("Segoe UI", 10), borderwidth=0)
        style.configure("Treeview.Heading", background="#334155", foreground="#f8fafc", font=("Segoe UI", 10, "bold"), relief="flat", padding=6)
        style.map("Treeview", background=[("selected", "#2563eb")], foreground=[("selected", "#ffffff")])
        style.configure("TProgressbar", thickness=10, troughcolor="#1e293b", background="#3b82f6", borderwidth=0)

    def _construir_ui(self):
        header = tk.Frame(self.root, bg="#1e293b", padx=20, pady=14)
        header.pack(fill="x", padx=14, pady=(14, 8))

        titulo_box = tk.Frame(header, bg="#1e293b")
        titulo_box.pack(side="left")

        tk.Label(
            titulo_box, text="SISTEMA DE MONITOREO ACÚSTICO Y VERIFICACIÓN DE KPIs",
            bg="#1e293b", fg="#f8fafc", font=("Segoe UI", 15, "bold")
        ).pack(anchor="w")
        tk.Label(
            titulo_box,
            text="Procesamiento multiseñal en tiempo real • Tiempos, precisión, falsas alarmas y envío de alertas",
            bg="#1e293b", fg="#94a3b8", font=("Segoe UI", 10)
        ).pack(anchor="w", pady=(2, 0))

        estado_ia_box = tk.Frame(header, bg="#1e293b")
        estado_ia_box.pack(side="right")

        self.lbl_ia_estado = tk.Label(
            estado_ia_box, text="IA: Verificando...", bg="#334155", fg="#e2e8f0",
            font=("Segoe UI", 9, "bold"), padx=12, pady=6
        )
        self.lbl_ia_estado.pack(side="right", padx=(8, 0))
        self.lbl_sensores_estado = tk.Label(
            estado_ia_box, text="📡 Sensores: OFF", bg="#334155", fg="#e2e8f0",
            font=("Segoe UI", 9, "bold"), padx=12, pady=6
        )
        self.lbl_sensores_estado.pack(side="right", padx=(8, 0))
        self.lbl_analisis = tk.Label(
            estado_ia_box, text="Análisis: ...", bg="#334155", fg="#e2e8f0",
            font=("Segoe UI", 9, "bold"), padx=12, pady=6, cursor="hand2"
        )
        self.lbl_analisis.pack(side="right", padx=(8, 0))
        self.lbl_cola = tk.Label(estado_ia_box, text="🧠 Cola IA: 0", bg="#334155", fg="#e2e8f0",
                                 font=("Segoe UI", 9, "bold"), padx=12, pady=6)
        self.lbl_cola.pack(side="right", padx=(8, 0))
        self.lbl_analisis.bind("<Button-1>", lambda _e: self.abrir_admin())

        # Barra de acciones con desplazamiento horizontal: en pantallas pequeñas algunos botones (Administración,
        # Probar IA...) quedaban fuera de la vista. Se mueve con la barrita de abajo o con la rueda del mouse.
        contenedor = tk.Frame(self.root, bg="#1e293b")
        contenedor.pack(fill="x", padx=14, pady=(0, 10))
        lienzo = tk.Canvas(contenedor, bg="#1e293b", highlightthickness=0, height=60)
        desliz = ttk.Scrollbar(contenedor, orient="horizontal", command=lienzo.xview)
        lienzo.configure(xscrollcommand=desliz.set)
        lienzo.pack(fill="x", side="top")
        desliz.pack(fill="x", side="top")
        barra_acciones = tk.Frame(lienzo, bg="#1e293b", padx=16, pady=10)
        lienzo.create_window((0, 0), window=barra_acciones, anchor="nw")
        def _ajustar(_e=None):
            lienzo.configure(scrollregion=lienzo.bbox("all"), height=barra_acciones.winfo_reqheight())
        barra_acciones.bind("<Configure>", _ajustar)
        def _rueda(ev):
            paso = -1 if (getattr(ev, "delta", 0) > 0 or getattr(ev, "num", 0) == 4) else 1
            lienzo.xview_scroll(paso * 3, "units")
            return "break"
        for w in (lienzo, barra_acciones):
            w.bind("<MouseWheel>", _rueda)
            w.bind("<Shift-MouseWheel>", _rueda)
            w.bind("<Button-4>", _rueda)
            w.bind("<Button-5>", _rueda)
        self._rueda_barra = _rueda

        self._boton(barra_acciones, "➕ Agregar Audios", self.accion_subir_audios, "#2563eb", "#1d4ed8").pack(side="left", padx=(0, 6))
        self._boton(barra_acciones, "📁 Cargar Carpeta", self.accion_cargar_carpeta, "#3b82f6", "#2563eb").pack(side="left", padx=6)
        self._boton(barra_acciones, "🧪 Generar 4 Señales de Prueba", self.accion_generar_prueba, "#8b5cf6", "#7c3aed").pack(side="left", padx=6)
        self._boton(barra_acciones, "⚡ Analizar Simultáneamente", self.accion_analizar, "#10b981", "#059669").pack(side="left", padx=6)
        self._boton(barra_acciones, "🔄 Re-analizar Todo", self.accion_reanalizar, "#0ea5e9", "#0284c7").pack(side="left", padx=6)

        self.btn_vigilar = self._boton(barra_acciones, "👁 Vigilancia Auto: OFF", self.accion_toggle_vigilar, "#475569", "#334155")
        self.btn_vigilar.pack(side="left", padx=6)

        config_box = tk.Frame(barra_acciones, bg="#0f172a", padx=10, pady=4)
        config_box.pack(side="left", padx=8)
        self._boton(barra_acciones, "📡 Sensores y SMS", self.abrir_sensores, "#0891b2", "#0e7490").pack(side="left", padx=6)
        self._boton(barra_acciones, "🔌 Probar IA", self.accion_verificar_ia, "#64748b", "#475569").pack(side="left", padx=6)
        self._boton(barra_acciones, "🗑 Limpiar Todo", self.accion_limpiar, "#ef4444", "#dc2626").pack(side="left", padx=(6, 0))

        self._boton(config_box, "🔒 Administración", self.abrir_admin, "#7c3aed", "#6d28d9").pack(side="left", padx=(0, 10))

        tk.Label(config_box, text="Señales simultáneas:", bg="#0f172a", fg="#cbd5e1", font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        spin = tk.Spinbox(
            config_box, from_=2, to=16, textvariable=self.workers_var, width=4,
            bg="#1e293b", fg="#f8fafc", buttonbackground="#334155", relief="flat", font=("Segoe UI", 10, "bold")
        )
        spin.pack(side="left")
        for w in list(barra_acciones.winfo_children()) + list(config_box.winfo_children()):
            w.bind("<MouseWheel>", self._rueda_barra, add="+")
            w.bind("<Shift-MouseWheel>", self._rueda_barra, add="+")

        kpi_frame = tk.Frame(self.root, bg="#0f172a")
        kpi_frame.pack(fill="x", padx=14, pady=(0, 10))
        for i in range(5):
            kpi_frame.columnconfigure(i, weight=1, uniform="kpi")

        self.card_det = self._crear_tarjeta_kpi(kpi_frame, 0, "KPI 1: TIEMPO DE DETECCIÓN", "Meta: <= 2.00 segundos")
        self.card_prec = self._crear_tarjeta_kpi(kpi_frame, 1, "KPI 2: PRECISIÓN DEL AUDIO", "Meta: >= 90% de aciertos")
        self.card_falsas = self._crear_tarjeta_kpi(kpi_frame, 2, "KPI 3: TASA FALSAS ALARMAS", "Meta: <= 10% reportes erróneos")
        self.card_alerta = self._crear_tarjeta_kpi(kpi_frame, 3, "KPI 4: TIEMPO ENVÍO ALERTA", "Meta: <= 10.00 segundos")
        self.card_paralelo = self._crear_tarjeta_kpi(kpi_frame, 4, "EJECUCIÓN SIMULTÁNEA", "Procesamiento multiseñal paralelo")

        progreso_frame = tk.Frame(self.root, bg="#1e293b", padx=14, pady=6)
        progreso_frame.pack(fill="x", padx=14, pady=(0, 10))

        self.lbl_progreso = tk.Label(
            progreso_frame, text="Estado: Listo para procesar múltiples señales al mismo tiempo.",
            bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")
        )
        self.lbl_progreso.pack(side="left")

        self.barra_progreso = ttk.Progressbar(progreso_frame, orient="horizontal", mode="determinate", length=360)
        self.barra_progreso.pack(side="right", padx=(10, 0))

        tabla_frame = tk.Frame(self.root, bg="#1e293b", padx=12, pady=10)
        tabla_frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        tk.Label(
            tabla_frame,
            text="VERIFICACIÓN DETALLADA POR SEÑAL DE AUDIO (TIEMPOS DE EJECUCIÓN, PRECISIÓN Y ALERTAS)",
            bg="#1e293b", fg="#f8fafc", font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", pady=(0, 6))

        columnas = (
            "id", "archivo", "hilo", "esperada", "detectada", "confianza",
            "verificacion", "alerta", "t_acustico", "t_ia", "t_alerta", "fuente"
        )
        self.tree = ttk.Treeview(tabla_frame, columns=columnas, show="headings", height=10)

        encabezados = [
            ("id", "ID", 50, "center"),
            ("archivo", "Archivo de Audio", 210, "w"),
            ("hilo", "Hilo Paralelo (Inicio)", 165, "center"),
            ("esperada", "Categoría Esperada", 130, "center"),
            ("detectada", "Categoría Detectada", 135, "center"),
            ("confianza", "Confianza", 85, "center"),
            ("verificacion", "Verificación KPI", 130, "center"),
            ("alerta", "Nivel Alerta", 95, "center"),
            ("t_acustico", "T. Detección Acústica", 135, "center"),
            ("t_ia", "T. Respuesta IA", 110, "center"),
            ("t_alerta", "T. Envío Alerta", 115, "center"),
            ("fuente", "Motor / Fuente", 140, "center")
        ]

        for col, texto, ancho, alin in encabezados:
            self.tree.heading(col, text=texto)
            self.tree.column(col, width=ancho, anchor=alin)

        self.tree.tag_configure("amenaza", background="#3f1d2e", foreground="#fecdd3")
        self.tree.tag_configure("falsa_alarma", background="#451a1a", foreground="#fca5a5")
        self.tree.tag_configure("correcto", background="#132f2c", foreground="#a7f3d0")
        self.tree.tag_configure("pendiente", background="#1e293b", foreground="#94a3b8")
        self.tree.tag_configure("normal", background="#1e293b", foreground="#f8fafc")
        self.menu_tabla = tk.Menu(self.root, tearoff=0)
        self.menu_tabla.add_command(label="▶ Escuchar audio", command=self._escuchar_fila)
        self.menu_tabla.add_separator()
        for cat in CATEGORIAS:
            self.menu_tabla.add_command(label=f"Categoría real: {cat}", command=lambda c=cat: self._etiquetar_fila(c))
        self.tree.bind("<Button-3>", self._menu_fila)
        self.tree.bind("<Button-2>", self._menu_fila)        # macOS

        scroll_y = ttk.Scrollbar(tabla_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll_y.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll_y.pack(side="right", fill="y")

        inferior = tk.Frame(self.root, bg="#0f172a")
        inferior.pack(fill="both", expand=False, padx=14, pady=(0, 14))
        inferior.columnconfigure(0, weight=1)
        inferior.columnconfigure(1, weight=1)

        panel_alertas = tk.Frame(inferior, bg="#1e293b", padx=12, pady=10)
        panel_alertas.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        tk.Label(
            panel_alertas, text="🚨 REPORTE DE ALERTAS EMITIDAS Y TIEMPO DE ENVÍO (META <= 10s)",
            bg="#1e293b", fg="#fca5a5", font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", pady=(0, 6))

        self.txt_alertas = tk.Text(
            panel_alertas, height=7, bg="#0f172a", fg="#fecdd3",
            font=("Consolas", 9), relief="flat", padx=8, pady=6, wrap="word"
        )
        self.txt_alertas.pack(fill="both", expand=True)

        panel_log = tk.Frame(inferior, bg="#1e293b", padx=12, pady=10)
        panel_log.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        tk.Label(
            panel_log, text="⚡ CONSOLA DE EJECUCIÓN SIMULTÁNEA EN TIEMPO REAL",
            bg="#1e293b", fg="#93c5fd", font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", pady=(0, 6))

        self.txt_log = tk.Text(
            panel_log, height=7, bg="#0f172a", fg="#e2e8f0",
            font=("Consolas", 9), relief="flat", padx=8, pady=6, wrap="word"
        )
        self.txt_log.pack(fill="both", expand=True)

    def _boton(self, parent, texto, comando, bg, active_bg):
        return tk.Button(
            parent, text=texto, command=comando, bg=bg, fg="#ffffff",
            activebackground=active_bg, activeforeground="#ffffff",
            font=("Segoe UI", 9, "bold"), relief="flat", padx=12, pady=7, cursor="hand2"
        )

    def _crear_tarjeta_kpi(self, parent, col, titulo, meta):
        card = tk.Frame(parent, bg="#1e293b", padx=14, pady=12, highlightthickness=1, highlightbackground="#334155")
        card.grid(row=0, column=col, sticky="nsew", padx=5)

        tk.Label(card, text=titulo, bg="#1e293b", fg="#94a3b8", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        lbl_valor = tk.Label(card, text="--", bg="#1e293b", fg="#f8fafc", font=("Segoe UI", 20, "bold"))
        lbl_valor.pack(anchor="w", pady=(6, 2))

        lbl_detalle = tk.Label(card, text=meta, bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9))
        lbl_detalle.pack(anchor="w", pady=(0, 8))

        lbl_badge = tk.Label(
            card, text="SIN DATOS", bg="#475569", fg="#ffffff",
            font=("Segoe UI", 9, "bold"), padx=10, pady=3
        )
        lbl_badge.pack(anchor="w")

        return {"card": card, "valor": lbl_valor, "detalle": lbl_detalle, "badge": lbl_badge}

    def _actualizar_tarjeta(self, tarjeta, valor_txt, detalle_txt, cumple):
        tarjeta["valor"].config(text=valor_txt)
        tarjeta["detalle"].config(text=detalle_txt)
        if cumple is None:
            tarjeta["badge"].config(text="PENDIENTE", bg="#475569")
            tarjeta["card"].config(highlightbackground="#334155")
        elif cumple:
            tarjeta["badge"].config(text="✓ CUMPLE KPI", bg="#16a34a")
            tarjeta["card"].config(highlightbackground="#16a34a")
        else:
            tarjeta["badge"].config(text="✗ NO CUMPLE", bg="#dc2626")
            tarjeta["card"].config(highlightbackground="#dc2626")

    def log(self, mensaje):
        def _escribir():
            self.txt_log.insert("end", mensaje + "\n")
            self.txt_log.see("end")
        self.root.after(0, _escribir)

    def _progreso(self, actual, total):
        def _act():
            porc = (actual / total * 100.0) if total else 0.0
            self.barra_progreso["value"] = porc
            self.lbl_progreso.config(text=f"Procesando señales en paralelo: {actual}/{total} completadas ({porc:.0f}%)")
        self.root.after(0, _act)

    def _verificar_ia_silencioso(self):
        def _tarea():
            try:
                _modelo_a_usar.cache_clear()
                modelos = listar_modelos()
                if modelos:
                    m = _modelo_a_usar()
                    self.root.after(0, lambda: self.lbl_ia_estado.config(text=f"🟢 LM Studio: {m.split('/')[-1][:16]}", bg="#065f46", fg="#d1fae5"))
                else:
                    self.root.after(0, lambda: self.lbl_ia_estado.config(text="🟡 LM Studio: Sin modelo cargado", bg="#92400e", fg="#fef3c7"))
            except Exception:
                self.root.after(0, lambda: self.lbl_ia_estado.config(text="⚪ LM Studio Offline", bg="#334155", fg="#cbd5e1"))
        threading.Thread(target=_tarea, daemon=True).start()

    def accion_verificar_ia(self):
        if not cargar_config().get("ia_activa", True) and not messagebox.askyesno(
                "Probar IA", "La IA está desactivada en Administración.\n¿Enviar igual 3 consultas de prueba a LM Studio?"):
            return
        def _tarea():
            d = diagnosticar_ia()
            if d["responde"]:
                self.log(f"LM Studio responde en {LMSTUDIO_URL} | Modelo: {d['modelo']} | {d['t_respuesta_s']} s | {d['mensaje']}")
                self.root.after(0, lambda: self.lbl_ia_estado.config(text=f"🟢 LM Studio: {d['modelo'].split('/')[-1][:16]}", bg="#065f46", fg="#d1fae5"))
                bien = d.get("aciertos", 3) == 3
                texto = f"{d['mensaje']}\n\nModelo: {d['modelo']}\nTiempo por consulta: {d['t_respuesta_s']} s\nRed neuronal de audio: {d['red_neuronal']}"
                self.root.after(0, lambda: (messagebox.showinfo("Conexión IA exitosa", texto) if bien
                                            else messagebox.showwarning("IA conectada, pero con respuestas incorrectas", texto)))
            else:
                self.log(f"Aviso IA: {d['mensaje']}")
                self.root.after(0, lambda: self.lbl_ia_estado.config(text="⚪ LM Studio sin respuesta", bg="#334155", fg="#cbd5e1"))
                self.root.after(0, lambda: messagebox.showwarning("IA no disponible", d["mensaje"] + "\n\nMientras tanto el sistema funciona con el motor acústico y la red neuronal de audio "
                                                                  f"({d['red_neuronal']}); las alertas no se detienen."))
        threading.Thread(target=_tarea, daemon=True).start()
    def _cambiar_modo_ia(self):
        global IA_MODO
        IA_MODO = "siempre" if self.ia_todas_var.get() else "auto"
        self.log(f"Modo IA: {'se consulta en todas las señales' if IA_MODO == 'siempre' else 'solo en señales ambiguas (más rápido)'}")
    def _refresco_diferido(self):
        """Lo llaman los hilos de sensores: refresca la ventana como máximo una vez por segundo."""
        if self._refresco_pendiente:
            return
        self._refresco_pendiente = True
        def _ir():
            self._refresco_pendiente = False
            try:
                self.actualizar_todo()
            except Exception:
                pass
        self.root.after(1000, _ir)
    def _estado_servidor(self):
        if self.servidor.activo:
            self.lbl_sensores_estado.config(text=f"📡 Sensores: ON :{self.servidor.puerto}", bg="#065f46", fg="#d1fae5")
        else:
            self.lbl_sensores_estado.config(text="📡 Sensores: OFF", bg="#334155", fg="#e2e8f0")
    def abrir_sensores(self):
        try:
            if self.ventana_sensores is not None and self.ventana_sensores.win.winfo_exists():
                self.ventana_sensores.win.lift()
                return
        except tk.TclError:
            pass
        self.ventana_sensores = VentanaSensores(self)
    def _cerrar(self):
        self.vigilando = False
        try:
            self.servidor.detener()
        except Exception:
            pass
        self.root.destroy()
    def accion_subir_audios(self):
        rutas = filedialog.askopenfilenames(
            title="Seleccionar señales de audio para monitoreo",
            filetypes=[("Archivos de Audio", "*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.opus *.wma"), ("Todos los archivos", "*.*")]
        )
        if not rutas:
            return
        agregados = 0
        for r in rutas:
            try:
                if subir_audio(Path(r), SENSOR_POR_DEFECTO) is not None:
                    agregados += 1
            except Exception as e:
                self.log(f"Error al subir {r}: {e}")
        self.log(f"Se registraron {agregados} nueva(s) señal(es) de audio. Iniciando análisis paralelo...")
        self.actualizar_todo()
        self.accion_analizar()

    def accion_cargar_carpeta(self):
        carpeta = filedialog.askdirectory(title="Seleccionar carpeta con señales de audio")
        if not carpeta:
            return
        carpeta_path = Path(carpeta)
        agregados = 0
        for r in sorted(carpeta_path.rglob("*")):
            if r.is_file() and r.suffix.lower() in EXTENSIONES_AUDIO:
                try:
                    if subir_audio(r, SENSOR_POR_DEFECTO) is not None:
                        agregados += 1
                except Exception as e:
                    self.log(f"Error en {r.name}: {e}")
        self.log(f"Carpeta escaneada: {agregados} señal(es) nueva(s) agregada(s).")
        self.actualizar_todo()
        if agregados > 0:
            self.accion_analizar()

    def accion_generar_prueba(self):
        creados = generar_audios_prueba()
        if creados == 0:
            reiniciar_analisis()
            self.log("Las 4 señales de prueba ya existían: se reinició su estado para analizarlas en paralelo.")
        else:
            self.log(f"Se generaron {creados} señales sintéticas calibradas (disparo, motosierra, motor_vehiculo, ambiente_bosque).")
        self.actualizar_todo()
        self.accion_analizar()

    def accion_analizar(self, desde_vigilancia=False):
        if self.procesando:
            return
        if not desde_vigilancia:
            permitido, motivo = estado_analisis()
            if not permitido and not messagebox.askyesno(
                    "Análisis detenido", f"El análisis automático está detenido: {motivo}.\n\n"
                    "¿Analizar igualmente los audios que cargaste a mano?"):
                return
        self.procesando = True
        usar_ia = self.usar_ia_var.get()
        workers = max(2, int(self.workers_var.get()))
        self.barra_progreso["value"] = 0
        self.lbl_progreso.config(text=f"Ejecutando análisis simultáneo con {workers} hilos paralelos...")

        def _tarea():
            try:
                res = procesar_pendientes_paralelo(
                    usar_ia=usar_ia,
                    max_workers=workers,
                    callback_log=self.log,
                    callback_progreso=self._progreso
                )
                if res["procesados"] > 0:
                    self.ultimo_lote = res
                    self.info_hilos.update(res.get("info_hilos", {}))
            finally:
                self.procesando = False
                self.root.after(0, self.actualizar_todo)
                self.root.after(0, lambda: self.lbl_progreso.config(text="Análisis simultáneo finalizado. Todos los KPIs han sido actualizados."))

        threading.Thread(target=_tarea, daemon=True).start()

    def accion_reanalizar(self):
        if self.procesando:
            return
        if contar("audios") == 0:
            messagebox.showinfo("Sin audios", "Primero agrega audios o presiona 'Generar 4 Señales de Prueba'.")
            return
        reiniciar_analisis()
        self.log("Se reiniciaron los análisis previos para recalcular todas las señales simultáneamente.")
        self.actualizar_todo()
        self.accion_analizar()

    def _encender_servidor_guardado(self):
        """Si el servidor de sensores estaba encendido la última vez, se vuelve a encender al abrir el programa
        (antes quedaba apagado y el celular mostraba «sin conexión» sin explicar por qué)."""
        cfg = cargar_config()
        if not cfg.get("servidor_encendido") or self.servidor.activo:
            return
        try:
            self.servidor.iniciar(int(cfg.get("servidor_puerto", 8000)), bool(cfg.get("servidor_red", False)))
            self.log("📡 Servidor de sensores encendido automáticamente (estaba encendido la última vez).")
        except Exception as e:
            self.log(f"⚠ No se pudo encender el servidor de sensores: {e}. Ábrelo en «Sensores y SMS».")
        self._estado_servidor()

    def _tick_cola(self):
        """Muestra cuántos audios esperan la revisión de la IA (se actualiza cada 2 s)."""
        try:
            n = COLA_IA.pendientes()
            espera = COLA_IA.en_espera() if n else None
            if espera and not COLA_IA.trabajando_en:
                self.lbl_cola.config(text=f"🧠 IA: {n} en cola ({'en pausa' if 'pausado' in espera else 'apagada' if 'desactivada' in espera else 'fuera de horario'})",
                                     bg="#334155", fg="#e2e8f0")
            elif COLA_IA.trabajando_en:
                self.lbl_cola.config(text=f"🧠 IA: {n} en cola", bg="#5b21b6", fg="#ede9fe")
            elif n:
                self.lbl_cola.config(text=f"🧠 IA: {n} en cola", bg="#92400e", fg="#fef3c7")
            else:
                self.lbl_cola.config(text="🧠 IA al día", bg="#334155", fg="#e2e8f0")
        except Exception:
            pass
        self.root.after(2000, self._tick_cola)

    def _menu_fila(self, ev):
        fila = self.tree.identify_row(ev.y)
        if fila:
            self.tree.selection_set(fila)
            self.menu_tabla.tk_popup(ev.x_root, ev.y_root)

    def _id_fila(self):
        sel = self.tree.selection()
        return int(str(self.tree.item(sel[0], "values")[0]).lstrip("#")) if sel else None

    def _escuchar_fila(self):
        aid = self._id_fila()
        with conectar() as bd:
            f = bd.execute("SELECT ruta FROM audios WHERE id=?", (aid,)).fetchone()
        if f and Path(f["ruta"]).exists():
            try:
                if os.name == "nt":
                    os.startfile(f["ruta"])                  # abre el reproductor de Windows
                else:
                    subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", f["ruta"]])
            except Exception as e:
                messagebox.showerror("Escuchar", str(e))

    def _etiquetar_fila(self, categoria):
        aid = self._id_fila()
        if aid is None:
            return
        try:
            destino = etiquetar_audio(aid, categoria)
        except Exception as e:
            messagebox.showerror("Etiquetar", str(e))
            return
        self.log(f"🏷 Audio #{aid} marcado como {categoria.upper()} (copiado a {destino.parent.name}/ para entrenar).")
        self.actualizar_todo()

    def _refrescar_estado_admin(self):
        """Actualiza el indicador de la cabecera (el horario cambia solo con la hora)."""
        try:
            cfg = cargar_config()
            permitido, motivo = estado_analisis(cfg)
            ia = "IA on" if cfg.get("ia_activa", True) else "IA off"
            if permitido:
                self.lbl_analisis.config(text=f"🟢 Análisis ON · {ia}", bg="#065f46", fg="#d1fae5")
            elif "horario" in motivo:
                self.lbl_analisis.config(text=f"🌙 Fuera de horario · {ia}", bg="#1e3a8a", fg="#dbeafe")
            else:
                self.lbl_analisis.config(text=f"⏸ PAUSADO · {ia}", bg="#92400e", fg="#fef3c7")
            self.usar_ia_var.set(bool(cfg.get("ia_activa", True)))
        except Exception:
            pass
        self.root.after(20000, self._refrescar_estado_admin)

    def abrir_admin(self):
        try:
            if self.ventana_admin is not None and self.ventana_admin.win.winfo_exists():
                self.ventana_admin.win.lift()
                return
        except Exception:
            pass
        pin = ""
        if hay_pin():
            pin = simpledialog.askstring("Administración", "PIN de administrador:", show="*", parent=self.root)
            if pin is None:
                return
            if not verificar_pin(pin):
                messagebox.showerror("Administración", "PIN incorrecto.", parent=self.root)
                return
        self.ventana_admin = VentanaAdmin(self, pin)

    def accion_toggle_vigilar(self):
        self.vigilando = not self.vigilando
        if self.vigilando:
            self.btn_vigilar.config(text="👁 Vigilancia Auto: ON", bg="#059669")
            self.log(f"Vigilancia automática activada sobre: {DIR_AUDIOS}")
            self._ciclo_vigilancia()
        else:
            self.btn_vigilar.config(text="👁 Vigilancia Auto: OFF", bg="#475569")
            self.log("Vigilancia automática desactivada.")

    def _ciclo_vigilancia(self):
        if not self.vigilando:
            return
        permitido, motivo = estado_analisis()
        if not permitido:
            if getattr(self, "_aviso_vigilancia", "") != motivo:
                self._aviso_vigilancia = motivo
                self.log(f"⏸ Vigilancia en espera: {motivo}.")
            self.root.after(3000, self._ciclo_vigilancia)
            return
        self._aviso_vigilancia = ""
        if not self.procesando:
            nuevos = escanear_entrada()
            pendientes = audios_pendientes()
            if nuevos > 0 or pendientes:
                self.accion_analizar(desde_vigilancia=True)
        self.root.after(3000, self._ciclo_vigilancia)

    def accion_limpiar(self):
        if self.procesando:
            messagebox.showinfo("Limpiar Todo", "Hay un análisis en curso. Espera a que termine y vuelve a pulsar «Limpiar Todo».")
            return
        if not messagebox.askyesno("Confirmar limpieza",
                                   "¿Borrar todos los audios, análisis, alertas, historial de mensajes y la cola de la IA?\n\n"
                                   "Se conservan los sensores, los destinatarios, la configuración y los audios de entrenamiento."):
            return
        estaba_vigilando = self.vigilando
        self.vigilando = False                              # que la vigilancia no vuelva a registrar nada mientras tanto
        try:
            borrados, fallidos = limpiar_todo()
        except Exception as e:
            messagebox.showerror("Limpiar Todo", f"No se pudo limpiar la base de datos: {e}")
            self.vigilando = estaba_vigilando
            return
        finally:
            if estaba_vigilando:
                self.vigilando = True
                self.root.after(3000, self._ciclo_vigilancia)
        if fallidos:
            messagebox.showwarning("Limpiar Todo", f"La base de datos quedó vacía. {len(fallidos)} archivo(s) de audio están "
                                   "abiertos en otro programa (reproductor, antivirus...) y no se pudieron borrar del disco, "
                                   "pero ya no aparecerán en la tabla.")
        self.info_hilos.clear()
        self.ultimo_lote = {"procesados": 0, "t_paralelo_s": 0.0, "t_secuencial_s": 0.0, "workers": self.workers_var.get()}
        self.txt_log.delete("1.0", "end")
        self.log(f"🗑 Limpieza completa: base de datos vacía y {borrados} archivo(s) de audio borrados.")
        self.lbl_progreso.config(text="Base de datos limpia. Listo para analizar nuevas señales.")
        self.barra_progreso["value"] = 0
        self.actualizar_todo()

    def actualizar_todo(self):
        escanear_entrada()
        k = calcular_kpis_actuales()

        if k["prom_deteccion_s"] is not None:
            self._actualizar_tarjeta(
                self.card_det,
                f"{k['prom_deteccion_s']:.3f} s",
                f"Meta <= {KPI_TIEMPO_DETECCION_S:.1f}s | Acústica: {k['prom_acustico_s']:.3f}s | IA: {k['prom_ia_s']:.3f}s",
                k["cumple_deteccion"]
            )
        else:
            self._actualizar_tarjeta(self.card_det, "--", f"Meta: <= {KPI_TIEMPO_DETECCION_S:.1f} segundos", None)

        if k["precision"] is not None:
            self._actualizar_tarjeta(
                self.card_prec,
                f"{k['precision']:.1%}",
                f"Meta >= {KPI_PRECISION_MIN:.0%} | Aciertos: {k['aciertos']}/{k['total_etiquetados']} señales",
                k["cumple_precision"]
            )
        else:
            self._actualizar_tarjeta(self.card_prec, "--", f"Meta: >= {KPI_PRECISION_MIN:.0%} (archivos con etiqueta)", None)

        if k["tasa_falsas"] is not None:
            self._actualizar_tarjeta(
                self.card_falsas,
                f"{k['tasa_falsas']:.1%}",
                f"Meta <= {KPI_FALSAS_ALARMAS_MAX:.0%} | Falsas alarmas: {k['falsas']}/{k['total_inocuos']} inocuos",
                k["cumple_falsas"]
            )
        else:
            self._actualizar_tarjeta(self.card_falsas, "--", f"Meta: <= {KPI_FALSAS_ALARMAS_MAX:.0%} en sonidos inocuos", None)

        if k["prom_alerta_s"] is not None and k["total_analisis"] > 0:
            self._actualizar_tarjeta(
                self.card_alerta,
                f"{k['prom_alerta_s']:.3f} s",
                f"Meta <= {KPI_TIEMPO_ALERTA_S:.1f}s | Alertas emitidas: {k['total_alertas']}",
                k["cumple_alerta"]
            )
        else:
            self._actualizar_tarjeta(self.card_alerta, "--", f"Meta: <= {KPI_TIEMPO_ALERTA_S:.1f} segundos", None)

        lote = self.ultimo_lote
        if lote["procesados"] > 0 and lote["t_paralelo_s"] > 0:
            acel = lote["t_secuencial_s"] / lote["t_paralelo_s"]
            self._actualizar_tarjeta(
                self.card_paralelo,
                f"{lote['t_paralelo_s']:.2f} s ({acel:.1f}x)",
                f"{lote['procesados']} señales en {lote['workers']} hilos (Sec: {lote['t_secuencial_s']:.2f}s)",
                lote["workers"] >= 2
            )
        else:
            self._actualizar_tarjeta(
                self.card_paralelo,
                f"{self.workers_var.get()} Hilos",
                "Listo para analizar >= 2 señales a la vez",
                None
            )

        for item in self.tree.get_children():
            self.tree.delete(item)

        filas = resumen_resultados()
        for f in filas:
            aid = f["audio_id"]
            esp = f["etiqueta"] or _esperada(f["nombre_archivo"])
            esp_txt = esp.upper() if esp else "N/A"
            det = f["categoria"]
            det_txt = det.upper() if det else "PENDIENTE"
            conf = f"{f['confianza']:.1%}" if f["confianza"] is not None else "--"
            alerta = (f["nivel_alerta"] or "--").upper()
            t_ac = f"{f['t_audio_s']:.3f} s" if f["t_audio_s"] is not None else "--"
            if f["ia_estado"] == "pendiente":
                t_ia = "🧠 analizando" if COLA_IA.trabajando_en == f["analisis_id"] else "⏳ en cola"
            elif f["ia_estado"] in ("fallo", "omitida"):
                t_ia = "falló" if f["ia_estado"] == "fallo" else "omitida"
            else:
                t_ia = f"{f['t_ia_s']:.3f} s" if f["t_ia_s"] else ("no necesaria" if f["categoria"] else "--")
            t_al = f"{f['t_alerta_s']:.3f} s" if f["t_alerta_s"] is not None else "No aplica"
            fuente = f["fuente"] or f["estado"]
            hilo_txt = self.info_hilos.get(aid, "Paralelo" if det else "En cola")

            if not det:
                verif = "PENDIENTE"
                tag = "pendiente"
            elif esp:
                es_falsa = (esp not in CATEGORIAS_AMENAZA) and (det in CATEGORIAS_AMENAZA) and ((f["confianza"] or 0.0) >= UMBRAL_CONFIANZA_ALERTA)
                if es_falsa:
                    verif = "✗ FALSA ALARMA"
                    tag = "falsa_alarma"
                elif det == esp:
                    verif = "✓ CORRECTO"
                    tag = "amenaza" if det in CATEGORIAS_AMENAZA else "correcto"
                else:
                    verif = "✗ DIFERENTE"
                    tag = "falsa_alarma"
            else:
                verif = "ANALIZADO"
                tag = "amenaza" if det in CATEGORIAS_AMENAZA else "normal"

            self.tree.insert(
                "", "end",
                values=(f"#{aid}", f["nombre_archivo"], hilo_txt, esp_txt, det_txt, conf, verif, alerta, t_ac, t_ia, t_al, fuente),
                tags=(tag,)
            )

        self.txt_alertas.delete("1.0", "end")
        activas = alertas_activas()
        if not activas:
            self.txt_alertas.insert("end", "No hay alertas de amenaza emitidas actualmente.\n")
        else:
            for al in activas:
                t_env = f"{al['t_alerta_s']:.3f}s" if al["t_alerta_s"] is not None else "--"
                cumple_env = "CUMPLE <=10s" if (al["t_alerta_s"] or 0.0) <= KPI_TIEMPO_ALERTA_S else "EXCEDE 10s"
                sms = estado_envio_alerta(al["id"])
                self.txt_alertas.insert(
                    "end",
                    f"[{al['fecha']}] [Tiempo de envío: {t_env} - {cumple_env}] [{sms}]\n  -> {al['mensaje']}\n\n"
                )


class VentanaAdmin:
    """Panel de administración: pausar/activar el análisis, apagar la IA (ahorra tokens), horario y PIN."""
    BG, PANEL, TXT, SUB = "#0f172a", "#1e293b", "#f8fafc", "#94a3b8"

    def __init__(self, app, pin):
        self.app, self.pin = app, pin
        cfg = cargar_config()
        self.win = tk.Toplevel(app.root)
        self.win.title("Administración del monitoreo")
        self.win.configure(bg=self.BG)
        self.win.geometry("720x700")
        self.win.transient(app.root)
        self.v_ia = tk.BooleanVar(value=cfg.get("ia_activa", True))
        self.v_ia_todas = tk.BooleanVar(value=cfg.get("ia_todas", False))
        self.v_horario = tk.BooleanVar(value=cfg.get("horario_activo", False))
        self.v_ini = tk.StringVar(value=cfg.get("horario_inicio", "18:00"))
        self.v_fin = tk.StringVar(value=cfg.get("horario_fin", "06:00"))

        p = self._panel("ANÁLISIS DE AUDIO")
        self.lbl_estado = tk.Label(p, text="", bg=self.PANEL, fg=self.TXT, font=("Segoe UI", 13, "bold"))
        self.lbl_estado.pack(anchor="w")
        tk.Label(p, text="Pausado: el audio de los sensores se recibe pero no se guarda ni se analiza (no gasta IA). "
                         "Las alertas tampoco se generan mientras esté pausado.",
                 bg=self.PANEL, fg=self.SUB, font=("Segoe UI", 9), wraplength=560, justify="left").pack(anchor="w", pady=(2, 8))
        self.btn = app._boton(p, "", self._alternar, "#16a34a", "#15803d")
        self.btn.pack(anchor="w")

        p = self._panel("INTELIGENCIA ARTIFICIAL (LM STUDIO)")
        tk.Checkbutton(p, text="Consultar la IA (si se apaga, la detección acústica y las alertas siguen funcionando)",
                       variable=self.v_ia, command=self._aplicar_ia, bg=self.PANEL, fg=self.TXT, selectcolor=self.BG, activebackground=self.PANEL,
                       activeforeground=self.TXT, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Checkbutton(p, text="Consultarla en TODAS las señales (gasta más tokens; por defecto solo en casos dudosos)",
                       variable=self.v_ia_todas, command=self._aplicar_ia, bg=self.PANEL, fg=self.TXT, selectcolor=self.BG, activebackground=self.PANEL,
                       activeforeground=self.TXT, font=("Segoe UI", 9)).pack(anchor="w")
        self.lbl_consultas = tk.Label(p, text="", bg=self.PANEL, fg=self.SUB, font=("Segoe UI", 9))
        self.lbl_consultas.pack(anchor="w", pady=(4, 0))

        p = self._panel("SENSORES: ENVIAR SOLO CUANDO HAYA SONIDO")
        self.v_sonido = tk.BooleanVar(value=cfg.get("solo_con_sonido", False))
        self.v_sens = tk.StringVar(value=cfg.get("sensibilidad_sonido", "media"))
        f2 = tk.Frame(p, bg=self.PANEL)
        f2.pack(anchor="w", fill="x")
        tk.Checkbutton(f2, text="Activado (los celulares y sensores no envían los trozos en silencio)", variable=self.v_sonido,
                       command=self._aplicar_sonido, bg=self.PANEL, fg=self.TXT, selectcolor=self.BG, activebackground=self.PANEL,
                       activeforeground=self.TXT, font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Label(f2, text="  Sensibilidad:", bg=self.PANEL, fg="#cbd5e1", font=("Segoe UI", 9)).pack(side="left")
        cb = ttk.Combobox(f2, textvariable=self.v_sens, values=["alta", "media", "baja"], state="readonly", width=7)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._aplicar_sonido())
        tk.Label(p, text="Alta: envía sonidos suaves o lejanos · Media: recomendado · Baja: solo sonidos fuertes. "
                         "Aunque haya silencio, cada sensor envía un trozo cada 2 min para confirmar que sigue funcionando.",
                 bg=self.PANEL, fg=self.SUB, font=("Segoe UI", 9), wraplength=640, justify="left").pack(anchor="w", pady=(4, 0))

        p = self._panel("HORARIO DE MONITOREO")
        tk.Checkbutton(p, text="Analizar solo dentro de este horario (fuera de él se comporta como pausado)",
                       variable=self.v_horario, bg=self.PANEL, fg=self.TXT, selectcolor=self.BG, activebackground=self.PANEL,
                       activeforeground=self.TXT, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        f = tk.Frame(p, bg=self.PANEL)
        f.pack(anchor="w", pady=(4, 0))
        for etiqueta, var in (("Desde (HH:MM):", self.v_ini), ("Hasta (HH:MM):", self.v_fin)):
            tk.Label(f, text=etiqueta, bg=self.PANEL, fg="#cbd5e1", font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
            tk.Entry(f, textvariable=var, width=7, bg=self.BG, fg=self.TXT, insertbackground=self.TXT, relief="flat",
                     font=("Segoe UI", 10)).pack(side="left", padx=(0, 14))
        tk.Label(p, text="Ejemplo: 18:00 a 06:00 analiza toda la noche (cruza la medianoche).",
                 bg=self.PANEL, fg=self.SUB, font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))

        b = tk.Frame(self.win, bg=self.BG)
        b.pack(fill="x", padx=12, pady=10)
        app._boton(b, "💾 Guardar", self._guardar, "#2563eb", "#1d4ed8").pack(side="left")
        app._boton(b, "🔑 Cambiar PIN", self._cambiar_pin, "#7c3aed", "#6d28d9").pack(side="left", padx=8)
        app._boton(b, "🎓 Entrenar con mis audios", self._entrenar, "#0891b2", "#0e7490").pack(side="left")
        self.lbl_msg = tk.Label(b, text="" if hay_pin() else "⚠ Sin PIN: cualquiera puede cambiar esto. Crea uno.",
                                bg=self.BG, fg="#fbbf24", font=("Segoe UI", 9))
        self.lbl_msg.pack(side="left", padx=8)
        self._pintar()

    def _panel(self, titulo):
        p = tk.Frame(self.win, bg=self.PANEL, padx=14, pady=10)
        p.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(p, text=titulo, bg=self.PANEL, fg=self.TXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
        return p

    def _pintar(self):
        cfg = cargar_config()
        permitido, motivo = estado_analisis(cfg)
        if cfg.get("analisis_activo", True):
            self.lbl_estado.config(text="🟢 ACTIVO" if permitido else f"🌙 En espera: {motivo}",
                                   fg="#86efac" if permitido else "#93c5fd")
            self.btn.config(text="⏸ Pausar análisis", bg="#d97706", activebackground="#b45309")
        else:
            self.lbl_estado.config(text="⏸ PAUSADO", fg="#fcd34d")
            self.btn.config(text="▶ Activar análisis", bg="#16a34a", activebackground="#15803d")
        try:
            self.lbl_consultas.config(text=f"Consultas a la IA hoy: {consultas_ia_hoy()}")
        except Exception:
            pass

    def _aplicar_sonido(self):
        try:
            cfg = administrar(self.pin, solo_con_sonido=self.v_sonido.get(), sensibilidad_sonido=self.v_sens.get())
        except Exception as e:
            messagebox.showerror("Administración", str(e), parent=self.win)
            return
        if cfg.get("solo_con_sonido"):
            self.app.log(f"🔒 Administrador: sensores envían SOLO CUANDO HAY SONIDO (sensibilidad {cfg['sensibilidad_sonido']}). "
                         f"Los celulares lo aplican en unos segundos.")
        else:
            self.app.log("🔒 Administrador: sensores envían audio CONTINUO (todos los trozos).")
        self.lbl_msg.config(text="Envío " + ("solo con sonido." if cfg.get("solo_con_sonido") else "continuo."), fg="#86efac")

    def _aplicar_ia(self):
        """Las casillas de la IA se aplican al marcarlas (antes solo con «Guardar»: si se cerraba la ventana sin
        guardar, la IA seguía recibiendo consultas)."""
        try:
            cfg = administrar(self.pin, ia_activa=self.v_ia.get(), ia_todas=self.v_ia_todas.get())
        except Exception as e:
            messagebox.showerror("Administración", str(e), parent=self.win)
            return
        self.app.ia_todas_var.set(bool(cfg.get("ia_todas")))
        if cfg["ia_activa"]:
            self.app.log(f"🔒 Administrador: IA ACTIVADA ({'todas las señales' if cfg.get('ia_todas') else 'solo casos dudosos'}).")
            COLA_IA.despertar()
        else:
            self.app.log("🔒 Administrador: IA DESACTIVADA. No se enviarán más consultas a LM Studio.")
        self.lbl_msg.config(text="IA " + ("activada." if cfg["ia_activa"] else "desactivada."), fg="#86efac")
        self._pintar()
        self.app._refrescar_estado_admin()

    def _alternar(self):
        nuevo = not cargar_config().get("analisis_activo", True)
        try:
            administrar(self.pin, analisis_activo=nuevo)
        except Exception as e:
            messagebox.showerror("Administración", str(e), parent=self.win)
            return
        self.app.log(f"🔒 Administrador: análisis {'ACTIVADO' if nuevo else 'PAUSADO'}.")
        self._pintar()
        self.app._refrescar_estado_admin()

    def _guardar(self):
        try:
            administrar(self.pin, ia_activa=self.v_ia.get(), ia_todas=self.v_ia_todas.get(),
                        horario_activo=self.v_horario.get(), horario_inicio=self.v_ini.get(), horario_fin=self.v_fin.get())
        except Exception as e:
            messagebox.showerror("Administración", str(e), parent=self.win)
            return
        cfg = cargar_config()
        self.v_ini.set(cfg["horario_inicio"]); self.v_fin.set(cfg["horario_fin"])
        self.app.ia_todas_var.set(bool(cfg.get("ia_todas")))
        self.app.log(f"🔒 Administrador: IA {'activada' if cfg['ia_activa'] else 'desactivada'}"
                     f"{' (todas las señales)' if cfg.get('ia_todas') else ''}; horario "
                     f"{cfg['horario_inicio'] + '-' + cfg['horario_fin'] if cfg['horario_activo'] else '24 h'}.")
        self.lbl_msg.config(text="Guardado." if hay_pin() else "Guardado. ⚠ Aún sin PIN.", fg="#86efac")
        self._pintar()
        self.app._refrescar_estado_admin()

    def _entrenar(self):
        carpeta = filedialog.askdirectory(parent=self.win, initialdir=str(DIR_ENTRENAMIENTO if DIR_ENTRENAMIENTO.exists() else RAIZ),
                                          title="Carpeta con subcarpetas por categoría (la carpeta 'entrenamiento' tiene los audios que marcaste)")
        if not carpeta:
            return
        script = RAIZ / "herramientas" / "entrenar_con_mis_audios.py"
        self.app.log(f"🎓 Entrenando con los audios de {carpeta} (puede tardar unos minutos)...")
        def _tarea():
            global _AJUSTE_CARGADO
            try:
                p = subprocess.Popen([sys.executable, str(script), carpeta], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", cwd=str(RAIZ))
                for linea in p.stdout:
                    self.app.root.after(0, lambda l=linea.rstrip(): self.app.log("  " + l))
                ok = p.wait() == 0
            except Exception as e:
                ok = False
                self.app.root.after(0, lambda: self.app.log(f"✗ No se pudo entrenar: {e}"))
            if ok:
                _AJUSTE_CARGADO = False          # el próximo análisis carga el modelo nuevo, sin reiniciar
            self.app.root.after(0, lambda: messagebox.showinfo(
                "Entrenamiento", "Listo: el sistema ya usa el modelo entrenado con sus audios. Revisa la precisión en la consola."
                if ok else "El entrenamiento no terminó. Revisa el mensaje en la consola.", parent=self.win))
        threading.Thread(target=_tarea, daemon=True).start()

    def _cambiar_pin(self):
        nuevo = simpledialog.askstring("PIN de administrador", "PIN nuevo (al menos 4 dígitos; vacío = quitar el PIN):",
                                       show="*", parent=self.win)
        if nuevo is None:
            return
        if nuevo and simpledialog.askstring("PIN de administrador", "Repite el PIN nuevo:", show="*", parent=self.win) != nuevo:
            messagebox.showerror("PIN", "Los PIN no coinciden.", parent=self.win)
            return
        try:
            cambiar_pin(self.pin, nuevo)
        except ValueError as e:
            messagebox.showerror("PIN", str(e), parent=self.win)
            return
        self.pin = nuevo
        self.lbl_msg.config(text="PIN actualizado." if nuevo else "PIN eliminado.", fg="#86efac")

class VentanaSensores:
    """Ventana secundaria: servidor de sensores, registro de sensores y configuración de SMS."""
    BG, PANEL, TXT, SUB = "#0f172a", "#1e293b", "#f8fafc", "#94a3b8"

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("Sensores en tiempo real y alertas por SMS")
        self.win.geometry("1000x860")
        self.win.minsize(900, 700)
        self.win.configure(bg=self.BG)
        cfg = cargar_config()
        self.var_puerto = tk.IntVar(value=int(cfg["servidor_puerto"]))
        self.var_red = tk.BooleanVar(value=bool(cfg["servidor_red"]))
        self.var_prov = tk.StringVar(value=cfg["sms_proveedor"])
        self.var_cool = tk.IntVar(value=int(cfg["sms_enfriamiento_s"]))
        self.var_nivel = tk.StringVar(value=cfg["sms_nivel_minimo"])
        self.var_sid = tk.StringVar(value=cfg["twilio_sid"])
        self.var_tok = tk.StringVar(value=cfg["twilio_token"])
        self.var_from = tk.StringVar(value=cfg["twilio_from"])
        self.var_hook = tk.StringVar(value=cfg["webhook_url"])
        self.var_and_url = tk.StringVar(value=cfg.get("android_url", ""))
        self.var_and_usr = tk.StringVar(value=cfg.get("android_usuario", ""))
        self.var_and_clv = tk.StringVar(value=cfg.get("android_clave", ""))
        self.var_tg_tok = tk.StringVar(value=cfg.get("telegram_token", ""))
        self.var_tg_chats = tk.StringVar(value=cfg.get("telegram_chats", ""))
        nb = ttk.Notebook(self.win)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab_s = tk.Frame(nb, bg=self.BG)
        self.tab_m = tk.Frame(nb, bg=self.BG)
        nb.add(self.tab_s, text="  📡 Sensores en tiempo real  ")
        nb.add(self.tab_m, text="  💬 Alertas por SMS  ")
        self._tab_sensores()
        self._tab_sms()
        self._refrescar()

    # ------------------------------------------------------------ utilidades
    def _titulo(self, parent, texto):
        tk.Label(parent, text=texto, bg=self.PANEL, fg=self.TXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 6))

    def _entrada(self, parent, etiqueta, variable, ancho=26, oculto=False, fila=None, col=0):
        tk.Label(parent, text=etiqueta, bg=self.PANEL, fg="#cbd5e1", font=("Segoe UI", 9)).grid(row=fila, column=col, sticky="w", padx=(0, 6), pady=3)
        e = tk.Entry(parent, textvariable=variable, width=ancho, bg=self.BG, fg=self.TXT, insertbackground=self.TXT,
                     relief="flat", font=("Segoe UI", 10), show="*" if oculto else "")
        e.grid(row=fila, column=col + 1, sticky="w", padx=(0, 14), pady=3)
        return e

    def _tabla(self, parent, columnas, altura):
        t = ttk.Treeview(parent, columns=[c[0] for c in columnas], show="headings", height=altura)
        for col, texto, ancho in columnas:
            t.heading(col, text=texto)
            t.column(col, width=ancho, anchor="w")
        return t

    # ------------------------------------------------------------ pestaña sensores
    def _tab_sensores(self):
        p = tk.Frame(self.tab_s, bg=self.PANEL, padx=14, pady=12)
        p.pack(fill="x", padx=8, pady=(8, 6))
        self._titulo(p, "SERVIDOR QUE RECIBE EL AUDIO DE LOS SENSORES")
        fila = tk.Frame(p, bg=self.PANEL)
        fila.pack(fill="x")
        tk.Label(fila, text="Puerto:", bg=self.PANEL, fg="#cbd5e1", font=("Segoe UI", 9)).pack(side="left")
        tk.Spinbox(fila, from_=1024, to=65535, textvariable=self.var_puerto, width=7, bg=self.BG, fg=self.TXT,
                   buttonbackground="#334155", relief="flat", font=("Segoe UI", 10, "bold")).pack(side="left", padx=(4, 14))
        tk.Checkbutton(fila, text="Aceptar sensores de otros equipos (red local)", variable=self.var_red, bg=self.PANEL, fg=self.TXT,
                       selectcolor=self.BG, activebackground=self.PANEL, activeforeground=self.TXT,
                       font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 14))
        self.btn_srv = self.app._boton(fila, "▶ Iniciar servidor", self._toggle_servidor, "#10b981", "#059669")
        self.btn_srv.pack(side="left")
        self.lbl_url = tk.Label(p, text="Servidor detenido.", bg=self.PANEL, fg=self.SUB, font=("Consolas", 9), justify="left")
        self.lbl_url.pack(anchor="w", pady=(8, 0))
        tk.Label(p, text="Cada sensor envía: POST a /audio con las cabeceras X-Sensor-Id y X-Sensor-Token y el archivo de audio en el cuerpo "
                         "(ver sensor_cliente.py). Si el sensor está en otro equipo, activa la casilla de red y permite el puerto en el firewall de Windows.",
                 bg=self.PANEL, fg=self.SUB, font=("Segoe UI", 9), wraplength=900, justify="left").pack(anchor="w", pady=(6, 0))

        q = tk.Frame(self.tab_s, bg=self.PANEL, padx=14, pady=12)
        q.pack(fill="both", expand=True, padx=8, pady=6)
        self._titulo(q, "SENSORES REGISTRADOS")
        self.tree_s = self._tabla(q, [("id", "ID", 90), ("nombre", "Nombre", 170), ("lat", "Latitud", 90), ("lon", "Longitud", 90),
                                      ("ultimo", "Último contacto", 150), ("n", "Audios", 60), ("estado", "Estado", 90)], 6)
        self.tree_s.pack(fill="both", expand=True)
        f = tk.Frame(q, bg=self.PANEL)
        f.pack(fill="x", pady=(10, 0))
        self.v_id, self.v_nom, self.v_lat, self.v_lon = tk.StringVar(), tk.StringVar(), tk.StringVar(value="4.5990"), tk.StringVar(value="-74.3130")
        self._entrada(f, "ID del sensor:", self.v_id, 12, fila=0, col=0)
        self._entrada(f, "Nombre:", self.v_nom, 20, fila=0, col=2)
        self._entrada(f, "Latitud:", self.v_lat, 12, fila=1, col=0)
        self._entrada(f, "Longitud:", self.v_lon, 12, fila=1, col=2)
        b = tk.Frame(q, bg=self.PANEL)
        b.pack(fill="x", pady=(10, 0))
        self.app._boton(b, "➕ Registrar sensor", self._registrar, "#2563eb", "#1d4ed8").pack(side="left", padx=(0, 6))
        self.app._boton(b, "📋 Copiar token", self._copiar_token, "#475569", "#334155").pack(side="left", padx=6)
        self.app._boton(b, "🔑 Regenerar token", self._regenerar, "#f59e0b", "#d97706").pack(side="left", padx=6)
        self.app._boton(b, "📱 Conectar celular", self._conectar_celular, "#7c3aed", "#6d28d9").pack(side="left", padx=6)
        self.lbl_msg_s = tk.Label(q, text="", bg=self.PANEL, fg="#86efac", font=("Segoe UI", 9), wraplength=880, justify="left")
        self.lbl_msg_s.pack(anchor="w", pady=(8, 0))

    def _toggle_servidor(self):
        srv = self.app.servidor
        try:
            if srv.activo:
                srv.detener()
                guardar_config({"servidor_encendido": False})       # apagado a propósito: no se enciende solo
            else:
                guardar_config({"servidor_puerto": int(self.var_puerto.get()), "servidor_red": bool(self.var_red.get()),
                                "servidor_encendido": True})
                srv.iniciar(int(self.var_puerto.get()), bool(self.var_red.get()))
        except OSError as e:
            messagebox.showerror("Servidor de sensores", f"No se pudo iniciar el servidor en ese puerto: {e}")
        except Exception as e:
            messagebox.showerror("Servidor de sensores", str(e))
        self.app._estado_servidor()
        self._refrescar_servidor()

    def _refrescar_servidor(self):
        srv = self.app.servidor
        if srv.activo:
            host = ip_local() if srv.red else "localhost"
            self.btn_srv.config(text="■ Detener servidor", bg="#ef4444", activebackground="#dc2626")
            extra = f"\n📱 Celulares: https://{host}:{srv.puerto_https}/celular" if getattr(srv, "puerto_https", None) else ""
            self.lbl_url.config(text=f"ACTIVO  ->  http://{host}:{srv.puerto}/audio   ({'red local' if srv.red else 'solo este equipo'}){extra}", fg="#86efac")
        else:
            self.btn_srv.config(text="▶ Iniciar servidor", bg="#10b981", activebackground="#059669")
            self.lbl_url.config(text="Servidor detenido.", fg=self.SUB)

    def _conectar_celular(self):
        """Usa un celular como sensor real: registra el sensor, inicia el servidor en red con HTTPS y muestra el código."""
        sid = simpledialog.askstring("Conectar celular", "ID para este celular-sensor (letras, números o guion):",
                                     initialvalue=self._sensor_seleccionado() or "CEL-01", parent=self.win)
        if not sid:
            return
        sid = sid.strip()
        try:
            if sid not in {x["id"] for x in listar_sensores_red()}:
                crear_sensor(sid, f"Celular {sid}", self.v_lat.get() or 4.599, self.v_lon.get() or -74.313)
            srv = self.app.servidor
            if srv.activo and not srv.red:
                srv.detener()                                # hay que aceptar conexiones de otros equipos
            if not srv.activo:
                self.var_red.set(True)
                self._toggle_servidor()          # guarda servidor_encendido=True
            if not srv.activo:
                return
            if not getattr(srv, "puerto_https", None):
                messagebox.showerror("Conectar celular", "No se pudo activar HTTPS, necesario para el micrófono del celular:\n"
                                     f"{srv.error_https}", parent=self.win)
                return
            codigo = crear_codigo_emparejamiento(sid)
        except Exception as e:
            messagebox.showerror("Conectar celular", str(e), parent=self.win)
            return
        self._refrescar()
        ips = ips_locales()
        v = tk.Toplevel(self.win)
        v.title("Conectar celular como sensor")
        v.configure(bg=self.BG)
        v.geometry("760x720")
        tk.Label(v, text="1. En el celular escribe esta dirección en la BARRA DE DIRECCIONES de Chrome o Safari\n"
                         "    (completa, con https:// y :" + str(srv.puerto_https) + "; no en el buscador de Google):",
                 bg=self.BG, fg=self.TXT, font=("Segoe UI", 11, "bold"), justify="left").pack(anchor="w", padx=18, pady=(18, 4))
        for i, ip in enumerate(ips[:4]):
            e = tk.Entry(v, font=("Consolas", 16 if i == 0 else 12, "bold"), bg=self.PANEL, readonlybackground=self.PANEL,
                         fg="#93c5fd" if i == 0 else "#64748b", relief="flat", justify="center")
            e.insert(0, f"https://{ip}:{srv.puerto_https}/celular" + ("" if i == 0 else "   (si la anterior no abre)"))
            e.config(state="readonly")
            e.pack(fill="x", padx=18, ipady=4 if i == 0 else 1, pady=(0, 2))
        fw = firewall_permitido()
        if fw is False:
            caja = tk.Frame(v, bg="#7f1d1d", padx=10, pady=6)
            caja.pack(fill="x", padx=18, pady=(8, 0))
            lbl_fw = tk.Label(caja, text="⚠ El firewall de Windows puede estar bloqueando al celular (causa más común de "
                                         "«no se encuentra la página»).", bg="#7f1d1d", fg="#fecaca",
                              font=("Segoe UI", 9, "bold"), wraplength=480, justify="left")
            lbl_fw.pack(side="left")
            def _permitir():
                try:
                    permitir_firewall([srv.puerto, srv.puerto_https])
                    lbl_fw.config(text="✓ Firewall configurado. Vuelve a cargar la página en el celular.", fg="#bbf7d0")
                    caja.config(bg="#14532d"); lbl_fw.config(bg="#14532d")
                except Exception as ex:
                    messagebox.showerror("Firewall", str(ex), parent=v)
            self.app._boton(caja, "🛡 Permitir (pide administrador)", _permitir, "#dc2626", "#b91c1c").pack(side="right")
        tk.Label(v, text="2. Aparecerá «La conexión no es privada»: es normal (certificado propio del programa). "
                         "Toca «Configuración avanzada» → «Continuar a ... (no seguro)».",
                 bg=self.BG, fg=self.SUB, font=("Segoe UI", 10), justify="left", wraplength=680).pack(anchor="w", padx=18, pady=(10, 0))
        tk.Label(v, text="3. Escribe este código (vence en 10 minutos, sirve una vez):", bg=self.BG, fg=self.TXT,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=18, pady=(14, 4))
        tk.Label(v, text=" ".join(codigo), bg=self.PANEL, fg="#fbbf24", font=("Consolas", 40, "bold")).pack(fill="x", padx=18, ipady=8)
        tk.Label(v, text="4. Pulsa «Empezar a escuchar» y permite el micrófono (y la ubicación, para que la alerta muestre dónde está).",
                 bg=self.BG, fg=self.TXT, font=("Segoe UI", 10), justify="left", wraplength=680).pack(anchor="w", padx=18, pady=(14, 0))
        tk.Label(v, text="Si no carga: el celular y este computador deben estar en la MISMA red Wi-Fi. Las redes de universidades "
                         "suelen bloquear la conexión entre equipos; en ese caso activa el punto de acceso (hotspot) del celular y "
                         "conecta el computador a él. La primera vez, Windows pregunta si permite a Python usar la red: acepta "
                         "(redes privadas).",
                 bg=self.BG, fg=self.SUB, font=("Segoe UI", 9), justify="left", wraplength=680).pack(anchor="w", padx=18, pady=(10, 0))
        self.app._boton(v, "Cerrar", v.destroy, "#475569", "#334155").pack(pady=14)

    def _sensor_seleccionado(self):
        sel = self.tree_s.selection()
        return self.tree_s.item(sel[0], "values")[0] if sel else None

    def _registrar(self):
        try:
            token = crear_sensor(self.v_id.get(), self.v_nom.get(), self.v_lat.get().replace(",", "."), self.v_lon.get().replace(",", "."))
        except ValueError as e:
            messagebox.showwarning("Registrar sensor", str(e))
            return
        self.win.clipboard_clear()
        self.win.clipboard_append(token)
        self.lbl_msg_s.config(text=f"Sensor {self.v_id.get()} registrado. Su token quedó copiado al portapapeles (también se ve al elegirlo en la tabla y pulsar 'Copiar token').")
        self._refrescar()

    def _copiar_token(self):
        sid = self._sensor_seleccionado()
        if not sid:
            messagebox.showinfo("Token", "Selecciona un sensor de la tabla.")
            return
        token = next((s["token"] for s in listar_sensores_red() if s["id"] == sid), "")
        self.win.clipboard_clear()
        self.win.clipboard_append(token)
        self.lbl_msg_s.config(text=f"Token del sensor {sid} copiado: {token}")

    def _regenerar(self):
        sid = self._sensor_seleccionado()
        if not sid:
            messagebox.showinfo("Token", "Selecciona un sensor de la tabla.")
            return
        if not messagebox.askyesno("Regenerar token", f"El sensor {sid} dejará de funcionar hasta que le configures el token nuevo. ¿Continuar?"):
            return
        token = regenerar_token(sid)
        self.win.clipboard_clear()
        self.win.clipboard_append(token)
        self.lbl_msg_s.config(text=f"Token nuevo del sensor {sid} copiado al portapapeles.")

    # ------------------------------------------------------------ pestaña SMS
    def _tab_sms(self):
        p = tk.Frame(self.tab_m, bg=self.PANEL, padx=14, pady=12)
        p.pack(fill="x", padx=8, pady=(8, 6))
        self._titulo(p, "PROVEEDOR DE MENSAJES DE TEXTO")
        g = tk.Frame(p, bg=self.PANEL)
        g.pack(fill="x")
        tk.Label(g, text="Proveedor:", bg=self.PANEL, fg="#cbd5e1", font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Combobox(g, textvariable=self.var_prov, values=["simulado", "android", "telegram", "twilio", "webhook"], state="readonly", width=14).grid(row=0, column=1, sticky="w", padx=(0, 14))
        tk.Label(g, text="Nivel mínimo:", bg=self.PANEL, fg="#cbd5e1", font=("Segoe UI", 9)).grid(row=0, column=2, sticky="w")
        ttk.Combobox(g, textvariable=self.var_nivel, values=["media", "alta"], state="readonly", width=8).grid(row=0, column=3, sticky="w", padx=(0, 14))
        tk.Label(g, text="Espera entre SMS del mismo sensor (s):", bg=self.PANEL, fg="#cbd5e1", font=("Segoe UI", 9)).grid(row=0, column=4, sticky="w")
        tk.Spinbox(g, from_=0, to=3600, textvariable=self.var_cool, width=6, bg=self.BG, fg=self.TXT, buttonbackground="#334155",
                   relief="flat", font=("Segoe UI", 10)).grid(row=0, column=5, sticky="w", padx=6)
        self._entrada(g, "Twilio Account SID:", self.var_sid, 34, fila=1, col=0)
        self._entrada(g, "Twilio Auth Token:", self.var_tok, 34, oculto=True, fila=1, col=2)
        self._entrada(g, "Número emisor Twilio:", self.var_from, 20, fila=2, col=0)
        self._entrada(g, "URL del webhook:", self.var_hook, 34, fila=2, col=2)
        self._entrada(g, "Android: URL de la app:", self.var_and_url, 34, fila=3, col=0)
        self._entrada(g, "Android: usuario:", self.var_and_usr, 20, fila=3, col=2)
        self._entrada(g, "Android: contraseña:", self.var_and_clv, 20, oculto=True, fila=4, col=0)
        self._entrada(g, "Telegram: token del bot:", self.var_tg_tok, 34, oculto=True, fila=4, col=2)
        self._entrada(g, "Telegram: chats (ids):", self.var_tg_chats, 34, fila=5, col=0)
        self.app._boton(g, "🔎 Detectar chats", self._detectar_chats, "#0ea5e9", "#0284c7").grid(row=5, column=2, sticky="w", pady=3)
        tk.Label(p, text="«simulado» no envía nada (guarda el mensaje en el historial, ideal para la demostración). «android» envía SMS reales GRATIS "
                         "desde tu celular con la app «SMS Gateway for Android». «telegram» envía la alerta por un bot de Telegram (gratis, por internet). "
                         "«twilio» envía SMS reales (de pago para Colombia). «webhook» envía un POST JSON {to, message} a tu propio servicio o a una app de teléfono.",
                 bg=self.PANEL, fg=self.SUB, font=("Segoe UI", 9), wraplength=900, justify="left").pack(anchor="w", pady=(6, 6))
        b = tk.Frame(p, bg=self.PANEL)
        b.pack(fill="x")
        self.app._boton(b, "💾 Guardar configuración", self._guardar_sms, "#2563eb", "#1d4ed8").pack(side="left", padx=(0, 6))
        self.app._boton(b, "✉ Enviar SMS de prueba", self._probar_sms, "#10b981", "#059669").pack(side="left", padx=6)
        self.lbl_msg_m = tk.Label(b, text="", bg=self.PANEL, fg="#86efac", font=("Segoe UI", 9))
        self.lbl_msg_m.pack(side="left", padx=10)

        d = tk.Frame(self.tab_m, bg=self.PANEL, padx=14, pady=12)
        d.pack(fill="x", padx=8, pady=6)
        self._titulo(d, "DESTINATARIOS (guardabosques)")
        self.tree_d = self._tabla(d, [("id", "ID", 50), ("nombre", "Nombre", 220), ("tel", "Teléfono", 180)], 4)
        self.tree_d.pack(fill="x")
        f = tk.Frame(d, bg=self.PANEL)
        f.pack(fill="x", pady=(8, 0))
        self.v_dn, self.v_dt = tk.StringVar(), tk.StringVar()
        self._entrada(f, "Nombre:", self.v_dn, 20, fila=0, col=0)
        self._entrada(f, "Teléfono (+57...):", self.v_dt, 18, fila=0, col=2)
        self.app._boton(f, "➕ Agregar", self._agregar_dest, "#2563eb", "#1d4ed8").grid(row=0, column=4, padx=6)
        self.app._boton(f, "🗑 Quitar", self._quitar_dest, "#ef4444", "#dc2626").grid(row=0, column=5, padx=6)

        h = tk.Frame(self.tab_m, bg=self.PANEL, padx=14, pady=12)
        h.pack(fill="both", expand=True, padx=8, pady=(6, 8))
        self._titulo(h, "HISTORIAL DE MENSAJES")
        self.tree_h = self._tabla(h, [("fecha", "Fecha", 140), ("tel", "Teléfono", 120), ("estado", "Estado", 90), ("msg", "Mensaje / detalle", 520)], 6)
        self.tree_h.pack(fill="both", expand=True)

    def _guardar_sms(self):
        try:
            cfg = guardar_config({"sms_proveedor": self.var_prov.get(), "sms_nivel_minimo": self.var_nivel.get(),
                                  "sms_enfriamiento_s": int(self.var_cool.get()), "twilio_sid": self.var_sid.get().strip(),
                                  "twilio_token": self.var_tok.get().strip(), "twilio_from": self.var_from.get().strip(),
                                  "webhook_url": self.var_hook.get().strip(),
                                  "android_url": self.var_and_url.get().strip(), "android_usuario": self.var_and_usr.get().strip(),
                                  "android_clave": self.var_and_clv.get().strip(), "telegram_token": self.var_tg_tok.get().strip(),
                                  "telegram_chats": self.var_tg_chats.get().strip()})
        except Exception as e:
            messagebox.showerror("Configuración", str(e))
            return False
        if cfg.get("twilio_from") and cfg["twilio_from"] != self.var_from.get().strip():
            self.var_from.set(cfg["twilio_from"])            # se muestra el número ya corregido (con +)
        if cfg.get("twilio_sid") and cfg["twilio_sid"] != self.var_sid.get().strip():
            self.var_sid.set(cfg["twilio_sid"])              # se muestra el SID ya corregido (con AC)
        self.lbl_msg_m.config(text="Configuración guardada.")
        return True

    def _detectar_chats(self):
        token = self.var_tg_tok.get().strip()
        if not token:
            messagebox.showinfo("Telegram", "Primero pega el token del bot (te lo da @BotFather).", parent=self.win)
            return
        def _tarea():
            try:
                chats = detectar_chats_telegram(token)
            except Exception as e:
                self.win.after(0, lambda: messagebox.showerror("Telegram", str(e), parent=self.win))
                return
            def _mostrar():
                if not chats:
                    messagebox.showinfo("Telegram", "No hay chats todavía. Abre tu bot en Telegram, pulsa Iniciar y escríbele "
                                        "'hola' (o agrégalo a un grupo y escribe algo en el grupo). Luego vuelve a pulsar 'Detectar chats'.",
                                        parent=self.win)
                    return
                self.var_tg_chats.set(", ".join(chats))
                self._guardar_sms()
                messagebox.showinfo("Telegram", "Chats detectados y guardados:\n" +
                                    "\n".join(f"{k}  {v}" for k, v in chats.items()), parent=self.win)
            self.win.after(0, _mostrar)
        threading.Thread(target=_tarea, daemon=True).start()

    def _probar_sms(self):
        if not self._guardar_sms():
            return
        if self.var_prov.get() != "telegram" and not listar_destinatarios():
            messagebox.showinfo("SMS de prueba", "Agrega al menos un destinatario.")
            return
        def _tarea():
            ok, err = sms_de_prueba()
            causas = []
            if err:
                with conectar() as bd:
                    causas = [f"{r['telefono']}: {r['detalle']}" for r in bd.execute(
                        "SELECT telefono, detalle FROM sms_log WHERE estado='error' ORDER BY id DESC LIMIT ?", (err,))]
            def _mostrar():
                self.lbl_msg_m.config(text=f"Prueba: {ok} enviado(s), {err} error(es).")
                self._refrescar()
                if causas:
                    messagebox.showerror("SMS de prueba: error", "\n\n".join(causas), parent=self.win)
            self.win.after(0, _mostrar)
        threading.Thread(target=_tarea, daemon=True).start()

    def _agregar_dest(self):
        try:
            agregar_destinatario(self.v_dn.get(), self.v_dt.get())
        except ValueError as e:
            messagebox.showwarning("Destinatario", str(e))
            return
        self.v_dn.set("")
        self.v_dt.set("")
        self._refrescar()

    def _quitar_dest(self):
        sel = self.tree_d.selection()
        if sel:
            borrar_destinatario(int(self.tree_d.item(sel[0], "values")[0]))
            self._refrescar()

    # ------------------------------------------------------------ refresco periódico
    def _refrescar(self):
        try:
            if not self.win.winfo_exists():
                return
        except tk.TclError:
            return
        self._refrescar_servidor()
        for t in (self.tree_s, self.tree_d, self.tree_h):
            for i in t.get_children():
                t.delete(i)
        ahora = time.time()
        for s in listar_sensores_red():
            u = s["ultimo_contacto"]
            estado = "inactivo" if not s["activo"] else ("EN LÍNEA" if u and ahora - u < 120 else "sin señal")
            self.tree_s.insert("", "end", values=(s["id"], s["nombre"], f"{s['latitud']:.5f}", f"{s['longitud']:.5f}",
                                                  time.strftime("%H:%M:%S", time.localtime(u)) if u else "nunca", s["audios_recibidos"], estado))
        for d in listar_destinatarios():
            self.tree_d.insert("", "end", values=(d["id"], d["nombre"], d["telefono"]))
        with conectar() as bd:
            for r in bd.execute("SELECT * FROM sms_log ORDER BY id DESC LIMIT 25"):
                # si falló, lo útil es la CAUSA (detalle), no el texto del SMS
                texto = r["detalle"] if r["estado"] in ("error", "omitido", "sin_destinatarios") else (r["mensaje"] or r["detalle"])
                self.tree_h.insert("", "end", values=(r["fecha"], r["telefono"], r["estado"], (texto or "")[:160]))
        self.win.after(3000, self._refrescar)

def lanzar_gui():
    if tk is None:
        raise SystemExit("Esta instalación de Python no incluye Tkinter. Reinstala Python marcando 'tcl/tk' o usa los comandos de consola.")
    root = tk.Tk()
    InterfazMonitoreo(root)
    root.mainloop()


def main():
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1].lower() == "gui"):
        lanzar_gui()
        return

    parser = argparse.ArgumentParser(description="Monitoreo acústico e IA")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("gui").set_defaults(f=lambda _: lanzar_gui())
    sub.add_parser("iniciar").set_defaults(f=cmd_iniciar)
    sub.add_parser("verificar-ia").set_defaults(f=cmd_verificar)

    s = sub.add_parser("subir")
    s.add_argument("audio")
    s.add_argument("--sensor", default=SENSOR_POR_DEFECTO)
    s.set_defaults(f=cmd_subir)

    s = sub.add_parser("analizar")
    s.add_argument("--sin-ia", action="store_true")
    s.set_defaults(f=cmd_analizar)

    s = sub.add_parser("vigilar")
    s.add_argument("--intervalo", type=float, default=5.0)
    s.add_argument("--sin-ia", action="store_true")
    s.set_defaults(f=cmd_vigilar)

    s = sub.add_parser("servidor", help="recibir audio de sensores por red sin abrir la ventana")
    s.add_argument("--puerto", type=int, default=cargar_config()["servidor_puerto"])
    s.add_argument("--red", action="store_true")
    s.add_argument("--sin-ia", action="store_true")
    s.set_defaults(f=cmd_servidor)
    s = sub.add_parser("sensor", help="registrar un sensor y obtener su token")
    s.add_argument("id")
    s.add_argument("--nombre", default="")
    s.add_argument("--lat", type=float, required=True)
    s.add_argument("--lon", type=float, required=True)
    s.set_defaults(f=cmd_sensor)
    s = sub.add_parser("destinatario", help="agregar un número que recibe los SMS")
    s.add_argument("nombre")
    s.add_argument("telefono")
    s.set_defaults(f=cmd_destinatario)
    sub.add_parser("sms-prueba").set_defaults(f=cmd_sms_prueba)
    sub.add_parser("firewall", help="permitir en el firewall de Windows los puertos de sensores y celulares").set_defaults(f=cmd_firewall)
    s = sub.add_parser("celular", help="conectar un celular como sensor (muestra la dirección y un código)")
    s.add_argument("id", nargs="?", default="CEL-01")
    s.add_argument("--lat", type=float, default=4.5990)
    s.add_argument("--lon", type=float, default=-74.3130)
    s.set_defaults(f=cmd_celular)
    s = sub.add_parser("admin", help="pausar/activar el análisis y la IA, horario y PIN (requiere PIN si hay uno)")
    s.add_argument("accion", choices=["estado", "pausar", "activar", "ia-on", "ia-off", "horario", "sin-horario", "pin",
                                          "sonido-on", "sonido-off"])
    s.add_argument("valores", nargs="*", help="horario: INICIO FIN (p. ej. 18:00 06:00) | pin: PIN_NUEVO")
    s.add_argument("--pin", default="", help="PIN de administrador actual")
    s.set_defaults(f=cmd_admin)
    sub.add_parser("estado").set_defaults(f=cmd_estado)
    sub.add_parser("ruta").set_defaults(f=cmd_ruta)
    sub.add_parser("reporte").set_defaults(f=cmd_reporte)

    s = sub.add_parser("limpiar")
    s.add_argument("--si", action="store_true")
    s.set_defaults(f=cmd_limpiar)

    args = parser.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()