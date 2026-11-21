"""
clasificador_sonidos.py - Red neuronal que ESCUCHA el audio (YAMNet de Google, entrenada con AudioSet:
2 millones de clips etiquetados en 521 clases, entre ellas "Chainsaw", "Gunshot, gunfire", "Fire", "Crackle",
"Engine", "Rain", "Bird"...). Se ejecuta con NumPy puro: no necesita TensorFlow, GPU ni internet.

Uso:
    from clasificador_sonidos import ClasificadorSonidos
    ia = ClasificadorSonidos()                    # carga modelos/yamnet.npz (~3 MB) una sola vez
    r = ia.analizar(x, 16000)                     # x: audio mono float32 en [-1, 1]
    r["categorias"]   -> {"motosierra": 0.71, "disparo": 0.02, "incendio": 0.0, ...}  (máximo en el tiempo)
    r["top"]          -> [("Chainsaw", 0.71), ("Engine", 0.33), ...]                   (etiquetas originales)
    r["linea_tiempo"] -> [{"t": 0.0, "motosierra": 0.65, ...}, ...]                     (cada 0,48 s)

Modelo: YAMNet (c) Google, licencia Apache 2.0. https://github.com/tensorflow/models/tree/master/research/audioset/yamnet
"""
import csv
import threading
from pathlib import Path

import numpy as np

DIR_MODELOS = Path(__file__).resolve().parent / "modelos"
SR_MODELO = 16000
VENTANA = 400          # 25 ms
SALTO = 160            # 10 ms
CUADROS_PARCHE = 96    # 0,96 s por parche
SALTO_PARCHE = 48      # un parche cada 0,48 s

# Clases de AudioSet que se agrupan en las categorías del proyecto (índices de modelos/yamnet_clases.csv).
GRUPOS = {
    "motosierra": {"Chainsaw": 1.0, "Power tool": 0.55, "Sawing": 0.45, "Light engine (high frequency)": 0.35},
    "disparo": {"Gunshot, gunfire": 1.0, "Machine gun": 0.9, "Fusillade": 0.9, "Artillery fire": 0.7,
                "Cap gun": 0.6, "Explosion": 0.6, "Burst, pop": 0.35, "Bang": 0.35},
    "incendio": {"Fire": 1.0, "Crackle": 0.7},
    "motor_vehiculo": {"Vehicle": 0.8, "Motor vehicle (road)": 0.9, "Car": 0.8, "Truck": 0.9, "Motorcycle": 0.9,
                       "Engine": 0.7, "Medium engine (mid frequency)": 0.7, "Heavy engine (low frequency)": 0.8,
                       "Idling": 0.7, "Accelerating, revving, vroom": 0.8, "Aircraft": 0.6, "Helicopter": 0.6,
                       "Aircraft engine": 0.6, "Engine starting": 0.7},
    "ambiente_natural": {"Rain": 1.0, "Raindrop": 0.9, "Rain on surface": 1.0, "Wind": 1.0,
                         "Wind noise (microphone)": 0.9, "Rustling leaves": 1.0, "Stream": 1.0, "Water": 0.8,
                         "Waterfall": 1.0, "Thunderstorm": 1.0, "Thunder": 0.9, "Bird": 1.0,
                         "Bird vocalization, bird call, bird song": 1.0, "Chirp, tweet": 1.0, "Insect": 1.0,
                         "Cricket": 1.0, "Frog": 1.0, "Croak": 1.0, "Animal": 0.6, "Wild animals": 0.8,
                         "Outside, rural or natural": 0.8, "Silence": 0.7, "Owl": 1.0, "Crow": 1.0,
                         "Fowl": 0.8, "Chicken, rooster": 0.8, "Dog": 0.6, "Mosquito": 1.0, "Fly, housefly": 1.0,
                         "Bee, wasp, etc.": 1.0, "Buzz": 0.6, "Rodents, rats, mice": 0.8, "Walk, footsteps": 0.6},
    # Impulsos naturales que se parecen a un disparo: se usan para descartar falsas alarmas.
    "trueno_goteo": {"Thunder": 1.0, "Thunderstorm": 1.0, "Drip": 1.0, "Plop": 1.0},
    # Sonidos humanos o de máquinas que las reglas confunden con motosierra (tonales y sostenidos). Si la red los
    # reconoce con fuerza, una "motosierra" vista solo por las reglas se descarta.
    "otros_sonidos": {"Bell": 1.0, "Church bell": 1.0, "Change ringing (campanology)": 1.0, "Siren": 1.0,
                      "Civil defense siren": 1.0, "Police car (siren)": 1.0, "Ambulance (siren)": 1.0,
                      "Fire engine, fire truck (siren)": 1.0, "Emergency vehicle": 1.0, "Alarm": 1.0,
                      "Vehicle horn, car horn, honking": 1.0, "Air horn, truck horn": 1.0, "Traffic noise, roadway noise": 1.0,
                      "Aircraft": 1.0, "Helicopter": 1.0, "Fixed-wing aircraft, airplane": 1.0, "Jet engine": 1.0,
                      "Rail transport": 1.0, "Train": 1.0, "Speech": 1.0, "Music": 0.8},
}


class ClasificadorSonidos:
    _instancia = None
    _lock = threading.Lock()

    @classmethod
    def compartido(cls):
        """Una sola copia del modelo para todos los hilos (se carga la primera vez que se usa)."""
        with cls._lock:
            if cls._instancia is None:
                cls._instancia = cls()
            return cls._instancia

    def __init__(self, ruta_modelo=None, ruta_clases=None):
        ruta_modelo = Path(ruta_modelo or DIR_MODELOS / "yamnet.npz")
        ruta_clases = Path(ruta_clases or DIR_MODELOS / "yamnet_clases.csv")
        z = np.load(ruta_modelo)
        self.ventana = z["ventana"].astype(np.float32)
        self.mel = z["mel"].T.astype(np.float32).copy()           # [257, 64]
        self.mel_suma = z["mel_suma"].astype(np.float32)
        self.capas = []
        for i, (tipo, paso) in enumerate(z["capas"].tolist()):
            w = z[f"w{i}"].astype(np.float32) * z[f"s{i}"]
            if tipo == 0:                                          # convolución normal [salida, kh, kw, entrada]
                if w.shape[1] == 1:
                    w = w[:, 0, 0, :].T.copy()                     # 1x1 -> matriz [entrada, salida]
                else:
                    w = w.reshape(w.shape[0], -1).T.copy()        # 3x3 -> [kh*kw*entrada, salida]
            else:                                                  # depthwise [1, kh, kw, canales]
                w = w[0]
            self.capas.append((tipo, paso, w, z[f"b{i}"].astype(np.float32)))
        self.fc_w = (z["fc_w"].astype(np.float32) * z["fc_s"]).T.copy()   # [1024, 521]
        self.fc_b = z["fc_b"].astype(np.float32)
        with open(ruta_clases, encoding="utf-8") as f:
            self.clases = [fila["display_name"] for fila in csv.DictReader(f)]
        indice = {n: i for i, n in enumerate(self.clases)}
        self.grupos = {g: [(indice[n], p) for n, p in clases.items() if n in indice] for g, clases in GRUPOS.items()}

    # ------------------------------------------------------------------ preprocesamiento
    def log_mel(self, x):
        """Espectrograma log-mel idéntico al de YAMNet: ventanas de 25 ms cada 10 ms, 64 bandas 125-7500 Hz."""
        x = np.asarray(x, dtype=np.float32)
        if len(x) < VENTANA:
            x = np.pad(x, (0, VENTANA - len(x)))
        cuadros = np.lib.stride_tricks.sliding_window_view(x, VENTANA)[::SALTO]
        espectro = np.abs(np.fft.rfft(cuadros * self.ventana, n=512, axis=1)).astype(np.float32)
        return np.log(espectro @ self.mel + self.mel_suma)

    def parches(self, x):
        lm = self.log_mel(x)
        if len(lm) < CUADROS_PARCHE:                      # audio de menos de 1 s: se completa repitiendo el piso
            lm = np.pad(lm, ((0, CUADROS_PARCHE - len(lm)), (0, 0)), mode="edge")
        inicios = list(range(0, len(lm) - CUADROS_PARCHE + 1, SALTO_PARCHE))
        if inicios[-1] != len(lm) - CUADROS_PARCHE:
            inicios.append(len(lm) - CUADROS_PARCHE)
        return np.stack([lm[i:i + CUADROS_PARCHE] for i in inicios]), [i * SALTO / SR_MODELO for i in inicios]

    # ------------------------------------------------------------------ red (MobileNet v1)
    @staticmethod
    def _rellenar(x, paso):
        # Relleno "SAME" de TensorFlow para kernel 3x3
        h, w = x.shape[1:3]
        ph = max((-(-h // paso) - 1) * paso + 3 - h, 0)
        pw = max((-(-w // paso) - 1) * paso + 3 - w, 0)
        return np.pad(x, ((0, 0), (ph // 2, ph - ph // 2), (pw // 2, pw - pw // 2), (0, 0))), -(-h // paso), -(-w // paso)

    def _red(self, x, con_huella=False):
        x = x[..., None]                                    # [N, 96, 64, 1]
        for tipo, paso, w, b in self.capas:
            if tipo == 0 and w.shape[0] == x.shape[-1]:     # 1x1
                x = x @ w + b
            elif tipo == 0:                                 # 3x3 normal (solo la primera capa)
                xp, ho, wo = self._rellenar(x, paso)
                cols = [xp[:, i:i + paso * ho:paso, j:j + paso * wo:paso, :] for i in range(3) for j in range(3)]
                x = np.concatenate(cols, axis=-1) @ w + b
            else:                                           # 3x3 depthwise
                xp, ho, wo = self._rellenar(x, paso)
                y = np.zeros((x.shape[0], ho, wo, x.shape[-1]), dtype=np.float32)
                for i in range(3):
                    for j in range(3):
                        y += xp[:, i:i + paso * ho:paso, j:j + paso * wo:paso, :] * w[i, j]
                x = y + b
            np.clip(x, 0.0, 6.0, out=x)                     # ReLU6
        x = x.mean(axis=(1, 2))                            # "huella" de 1024 valores que describe el sonido
        prob = 1.0 / (1.0 + np.exp(-(x @ self.fc_w + self.fc_b)))
        return (prob, x) if con_huella else prob

    def puntajes(self, x, sr=SR_MODELO):
        """Probabilidad (0-1) de cada una de las 521 clases en cada parche de 0,96 s. Devuelve (matriz, tiempos)."""
        if sr != SR_MODELO:
            from scipy import signal
            g = np.gcd(int(sr), SR_MODELO)
            x = signal.resample_poly(x, SR_MODELO // g, int(sr) // g)
        p, t = self.parches(x)
        salida = [self._red(p[i:i + 16], con_huella=True) for i in range(0, len(p), 16)]   # en lotes: memoria acotada
        self._ultima_huella = np.concatenate([h for _, h in salida])
        return np.concatenate([s for s, _ in salida]), t

    # ------------------------------------------------------------------ resultado para el sistema
    def analizar(self, x, sr=SR_MODELO, n_top=5):
        s, tiempos = self.puntajes(x, sr)
        huellas = self._ultima_huella
        por_grupo = {g: np.max([s[:, i] * p for i, p in idx], axis=0) if idx else np.zeros(len(s))
                     for g, idx in self.grupos.items()}
        media = s.mean(axis=0)
        maximo = s.max(axis=0)
        orden = np.argsort(-(0.5 * media + 0.5 * maximo))[:n_top]
        return {
            "categorias": {g: round(float(v.max()), 3) for g, v in por_grupo.items()},
            "parches_con": {g: int((v >= 0.25).sum()) for g, v in por_grupo.items()},
            "n_parches": len(s),
            "top": [(self.clases[i], round(float(maximo[i]), 3)) for i in orden],
            # huella media y máxima de los parches (2048 valores): entrada del clasificador entrenado con audios propios
            "huella": np.concatenate([huellas.mean(axis=0), huellas.max(axis=0)]).astype(np.float32),
            "linea_tiempo": [{"t": round(t, 2), **{g: round(float(v[k]), 3) for g, v in por_grupo.items()}}
                             for k, t in enumerate(tiempos)],
        }


class AjusteLocal:
    """Clasificador entrenado con las grabaciones PROPIAS del proyecto (herramientas/entrenar_con_mis_audios.py)
    sobre la huella de YAMNet. Aprende cómo suenan sus micrófonos y su bosque."""

    def __init__(self, ruta=None):
        z = np.load(ruta or DIR_MODELOS / "ajuste_local.npz", allow_pickle=False)
        self.W, self.b = z["W"], z["b"]
        self.media, self.escala = z["media"], z["escala"]
        self.clases = [str(c) for c in z["clases"]]
        self.precision_cv = float(z["precision_cv"])
        self.n_audios = int(z["n_audios"])

    def probabilidades(self, huella):
        z = ((np.asarray(huella, dtype=np.float32) - self.media) / self.escala) @ self.W + self.b
        z = np.exp(z - z.max())
        return dict(zip(self.clases, (z / z.sum()).astype(float)))
