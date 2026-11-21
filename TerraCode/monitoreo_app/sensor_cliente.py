"""
sensor_cliente.py - Simula (o ejecuta) un sensor acústico que envía audio en tiempo real a la aplicación.

Ejemplos (el token lo entrega la ventana 'Sensores y SMS' al registrar el sensor):

  Simular un sensor con un archivo (lo corta en trozos de 5 s y los envía al ritmo real):
      python sensor_cliente.py --url http://localhost:8000 --id S01 --token TU_TOKEN --archivo bosque.wav

  Sensor con micrófono (necesita: pip install sounddevice):
      python sensor_cliente.py --url http://192.168.1.10:8000 --id S01 --token TU_TOKEN --microfono

Si no hay red, los trozos se guardan en memoria (máx. 60) y se reenvían cuando vuelva la conexión.

Si el administrador activa «Enviar solo cuando haya sonido» (programa > Administración), el sensor no envía los
trozos en silencio: aprende el ruido de fondo y envía solo cuando algo sobresale (más un trozo cada 2 min como latido).
"""
import argparse, io, sys, time, wave
from collections import deque
import numpy as np
import requests

SR = 16000

def a_wav_bytes(x, sr):
    x = np.clip(x, -1.0, 1.0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return buf.getvalue()

def trozos_de_archivo(ruta, seg):
    from scipy.io import wavfile
    sr, d = wavfile.read(ruta)
    if d.ndim > 1:
        d = d.astype(np.float64).mean(axis=1)
    d = d.astype(np.float32) / (np.iinfo(d.dtype).max + 1 if np.issubdtype(d.dtype, np.integer) else 1.0)
    n = int(seg * sr)
    for i in range(0, len(d), n):
        t = d[i:i + n]
        if len(t) >= sr:                      # se descartan colas menores a 1 s
            yield t, sr

def trozos_de_microfono(seg):
    try:
        import sounddevice as sd
    except ImportError:
        sys.exit("Para usar el micrófono instala: pip install sounddevice")
    while True:
        x = sd.rec(int(seg * SR), samplerate=SR, channels=1, dtype="float32")
        sd.wait()
        yield x[:, 0].copy(), SR

class DetectorSonido:
    """Decide si un trozo se envía cuando está activo «solo con sonido» (misma lógica que la página del celular)."""
    def __init__(self):
        self.cfg = {"solo_con_sonido": False, "umbral_db": 10.0, "latido_s": 120}
        self.piso, self.extra, self.ultimo = None, 0, 0.0

    def decidir(self, x, sr):
        x = np.asarray(x, dtype=np.float64)
        rms_db = 10 * np.log10(np.mean(x * x) + 1e-12)
        v = max(1, int(sr * 0.05))
        n = len(x) // v
        max_db = 10 * np.log10(np.max(np.mean(x[:n * v].reshape(n, v) ** 2, axis=1)) + 1e-12) if n else rms_db
        if not self.cfg.get("solo_con_sonido"):
            return True, ""
        if self.piso is None:
            self.piso = rms_db
            return True, "primer trozo"
        u = float(self.cfg.get("umbral_db", 10.0))
        activo = max_db > self.piso + u or rms_db > self.piso + u * 0.6
        self.piso = min(self.piso + 0.1, rms_db) if activo else 0.85 * self.piso + 0.15 * rms_db
        if activo:
            self.extra = 1
            return True, "sonido"
        if self.extra > 0:
            self.extra -= 1
            return True, "continuación"
        if time.time() - self.ultimo > float(self.cfg.get("latido_s", 120)):
            return True, "latido"
        return False, "silencio"

def main():
    p = argparse.ArgumentParser(description="Sensor acústico: envía audio a la aplicación de monitoreo")
    p.add_argument("--url", default="http://localhost:8000", help="dirección de la aplicación (sin /audio)")
    p.add_argument("--id", required=True, help="identificador del sensor registrado, p. ej. S01")
    p.add_argument("--token", required=True)
    p.add_argument("--archivo", help="archivo .wav a transmitir en trozos")
    p.add_argument("--microfono", action="store_true")
    p.add_argument("--trozo", type=float, default=5.0, help="segundos de audio por envío (mínimo recomendado: 2)")
    p.add_argument("--rapido", action="store_true", help="no esperar entre envíos (solo para pruebas)")
    p.add_argument("--repetir", action="store_true", help="repetir el archivo indefinidamente")
    a = p.parse_args()
    if not a.archivo and not a.microfono:
        p.error("indica --archivo o --microfono")
    url = a.url.rstrip("/") + "/audio"
    cab = {"X-Sensor-Id": a.id, "X-Sensor-Token": a.token}
    pendientes = deque(maxlen=60)
    n = 0
    detector = DetectorSonido()
    omitidos = 0
    ultima_cfg = 0.0

    def leer_config():
        try:
            r = requests.get(a.url.rstrip("/") + "/config", headers=cab, timeout=5)
            if r.ok:
                detector.cfg = r.json()
        except requests.RequestException:
            pass

    def enviar(datos, nombre, omit):
        extra = {"X-Omitidos": str(omit)} if omit else {}
        r = requests.post(url, data=datos, headers={**cab, "X-Filename": nombre, **extra}, timeout=15)
        if r.status_code == 401:
            sys.exit("El servidor rechazó el ID o el token del sensor.")
        r.raise_for_status()
        return r.json()

    def fuente():
        while True:
            if a.microfono:
                yield from trozos_de_microfono(a.trozo)
            else:
                yield from trozos_de_archivo(a.archivo, a.trozo)
                if not a.repetir:
                    return

    for x, sr in fuente():
        n += 1
        if time.time() - ultima_cfg > 30:                # la opción de Administración se aplica en ~30 s
            leer_config(); ultima_cfg = time.time()
        enviar_trozo, motivo = detector.decidir(x, sr)
        if not enviar_trozo:
            omitidos += 1
            print(f"[{time.strftime('%H:%M:%S')}] silencio: no se envía ({omitidos} sin enviar)")
            if a.archivo and not a.rapido:
                time.sleep(a.trozo)
            continue
        detector.ultimo = time.time()
        pendientes.append((a_wav_bytes(x, sr), f"{a.id}_{int(time.time())}_{n}.wav", omitidos))
        omitidos = 0
        while pendientes:
            d, nombre, omit = pendientes[0]
            try:
                r = enviar(d, nombre, omit)
                pendientes.popleft()
                if r.get("config"):
                    detector.cfg = r["config"]
                estado = ("duplicado" if r.get("duplicado") else
                          f"recibido SIN analizar ({r.get('motivo')})" if r.get("analizado") is False else "recibido")
                print(f"[{time.strftime('%H:%M:%S')}] enviado {nombre} -> {estado}")
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] sin conexión ({e.__class__.__name__}); {len(pendientes)} trozo(s) en espera")
                break
        if a.archivo and not a.rapido:
            time.sleep(a.trozo)

if __name__ == "__main__":
    main()
