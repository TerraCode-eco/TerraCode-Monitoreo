"""
Mide los KPI de precisión y falsas alarmas con AUDIOS REALES (no sintéticos) del dataset público ESC-50
(Piczak 2015, licencia CC BY-NC 3.0, https://github.com/karolpiczak/ESC-50). Útil para el informe del proyecto.

    python herramientas/evaluar_con_esc50.py                 # descarga ~130 MB la primera vez (folds 4 y 5)
    python herramientas/evaluar_con_esc50.py --por-clase 8   # más rápido: 8 audios por clase

Qué mide (los mismos criterios que la ventana de KPIs):
  - Detección de amenazas: motosierras (chainsaw) e incendios (crackling_fire) que generan alerta con la categoría correcta.
  - Falsas alarmas: sonidos naturales del bosque (lluvia, viento, aves, insectos, ranas, trueno...) que generan alerta.
  - Falsas alarmas con ruido humano/máquinas (aviones, sirenas, campanas, pólvora...) se informan aparte.
ESC-50 no trae disparos reales; para disparos usa tus propias grabaciones con "Cargar Carpeta".
No toca la base de datos del programa.
"""
import argparse
import collections
import csv
import io
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import monitoreo as m  # noqa: E402

BASE = "https://raw.githubusercontent.com/karolpiczak/ESC-50/master"
CARPETA = RAIZ / "base_de_datos" / "esc50"
AMENAZAS = {"chainsaw": "motosierra", "crackling_fire": "incendio"}
NATURALES = ["rain", "wind", "thunderstorm", "chirping_birds", "crickets", "insects", "frog", "water_drops",
             "pouring_water", "sea_waves", "crow", "rooster", "hen", "dog", "cow", "sheep", "pig", "footsteps",
             "door_wood_knock"]
HUMANOS = ["engine", "helicopter", "airplane", "train", "hand_saw", "siren", "car_horn", "church_bells",
           "clock_tick", "fireworks"]

def descargar(nombre):
    destino = CARPETA / nombre
    if not destino.exists() or destino.stat().st_size == 0:
        with urllib.request.urlopen(f"{BASE}/audio/{nombre}", timeout=60) as r:
            destino.write_bytes(r.read())
    return destino

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--por-clase", type=int, default=16, help="audios por clase (máx. 16 en los folds 4 y 5)")
    a = p.parse_args()
    CARPETA.mkdir(parents=True, exist_ok=True)
    print("Descargando la lista de ESC-50...")
    with urllib.request.urlopen(f"{BASE}/meta/esc50.csv", timeout=60) as r:
        filas = list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))
    clases = list(AMENAZAS) + NATURALES + HUMANOS
    elegidos, cuenta = [], collections.Counter()
    for f in filas:
        if f["category"] in clases and f["fold"] in ("4", "5") and cuenta[f["category"]] < a.por_clase:
            cuenta[f["category"]] += 1
            elegidos.append((f["filename"], f["category"]))
    print(f"Descargando {len(elegidos)} audios (solo la primera vez)...")
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda e: descargar(e[0]), elegidos))

    print(f"Red neuronal: {m.estado_red()}\nAnalizando...")
    res = collections.defaultdict(lambda: [0, 0, collections.Counter()])
    tiempos = []
    for nombre, clase in elegidos:
        x = m.cargar_audio(CARPETA / nombre)
        t0 = time.perf_counter()
        _, cat, conf, _, _ = m.analisis_acustico(x)
        tiempos.append(time.perf_counter() - t0)
        alerta = cat in m.CATEGORIAS_AMENAZA and conf >= m.UMBRAL_CONFIANZA_ALERTA
        r = res[clase]
        r[0] += 1
        r[1] += (cat == AMENAZAS[clase] and alerta) if clase in AMENAZAS else alerta
        r[2][cat] += 1

    print(f"\n{'clase':16} {'n':>3}  resultado")
    for clase in clases:
        n, v, cnt = res[clase]
        etiqueta = "detectadas" if clase in AMENAZAS else "falsas alarmas"
        print(f"{clase:16} {n:3}  {v:3} {etiqueta:15} {dict(cnt)}")

    def total(grupo):
        n = sum(res[c][0] for c in grupo)
        v = sum(res[c][1] for c in grupo)
        return v, n, (v / n if n else 0.0)

    print("\n=== KPI (audios reales) ===")
    for titulo, grupo, meta, mayor in (("Detección de motosierras", ["chainsaw"], m.KPI_PRECISION_MIN, True),
                                       ("Detección de incendios", ["crackling_fire"], None, True),
                                       ("Falsas alarmas (sonidos naturales)", NATURALES, m.KPI_FALSAS_ALARMAS_MAX, False),
                                       ("Falsas alarmas (ruido humano/máquinas)", HUMANOS, None, False)):
        v, n, t = total(grupo)
        cumple = "" if meta is None else ("  CUMPLE" if (t >= meta if mayor else t <= meta) else "  NO CUMPLE")
        print(f"{titulo:40} {v:3}/{n:<3} = {t:6.1%}{cumple}")
    print(f"{'Tiempo medio de detección':40} {sum(tiempos) / len(tiempos):.3f} s (máx. {max(tiempos):.3f} s)")

if __name__ == "__main__":
    main()
