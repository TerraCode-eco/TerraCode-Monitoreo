"""
Entrena el sistema con GRABACIONES PROPIAS (las de sus micrófonos en el parque) para que identifique mejor los sonidos.

Organiza los audios en una carpeta con una subcarpeta por categoría (o con la categoría en el nombre del archivo):

    mis_audios/
        motosierra/         sierra_lote3.wav, sierra_lejos.mp3 ...
        disparo/            ...
        incendio/           ...
        motor_vehiculo/     ...
        ambiente_natural/   lluvia_noche.wav, aves_mañana.wav, viento.wav ...   <- ¡muy importante!
        otro/               voces, música, perros de fincas vecinas ...

    python herramientas/entrenar_con_mis_audios.py mis_audios

Consejos: al menos 10 audios por categoría (más es mejor), grabados con SUS micrófonos y en SU bosque; incluye
muchos sonidos naturales normales (son los que causan falsas alarmas). Los audios largos se cortan en trozos de 5 s.

El programa mide la precisión con validación cruzada (prueba cada audio con un modelo que NO lo vio al entrenar) y
guarda modelos/ajuste_local.npz. monitoreo.py lo usa automáticamente al reiniciar. Para volver al comportamiento
anterior basta con borrar ese archivo.
"""
import argparse
import collections
import sys
import time
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import monitoreo as m  # noqa: E402

TROZO_S = 5.0

def etiqueta_de(ruta, base):
    partes = [p.lower() for p in ruta.relative_to(base).parts[:-1]]
    for p in partes:
        if p in m.CATEGORIAS:
            return p
        e = m._esperada(p + ".wav")
        if e:
            return e
    return m._esperada(ruta.name)

def huellas_de(ruta):
    x = m.cargar_audio(ruta)
    sr = m.FRECUENCIA_MUESTREO
    n, paso = int(TROZO_S * sr), int(TROZO_S * sr / 2)
    trozos = [x] if len(x) <= int(1.2 * n) else [x[i:i + n] for i in range(0, len(x) - n // 2, paso)]
    salida = []
    for t in trozos:
        if len(t) < sr:
            continue
        r = m.escuchar_con_red(t, sr)
        salida.append(r["huella"])
    return salida

def entrenar(X, y, k, lam=1e-2, iters=400, lr=0.05):
    """Regresión logística multiclase con regularización L2 y pesos por clase (Adam). NumPy puro."""
    n, d = X.shape
    W, b = np.zeros((d, k), np.float32), np.zeros(k, np.float32)
    Y = np.eye(k, dtype=np.float32)[y]
    peso = (n / (k * np.maximum(np.bincount(y, minlength=k), 1)))[y][:, None].astype(np.float32)
    mW = vW = np.zeros_like(W); mb = vb = np.zeros_like(b)
    for t in range(1, iters + 1):
        z = X @ W + b
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z); p /= p.sum(axis=1, keepdims=True)
        g = (p - Y) * peso / n
        gW, gb = X.T @ g + lam * W, g.sum(axis=0)
        mW = 0.9 * mW + 0.1 * gW; vW = 0.999 * vW + 0.001 * gW ** 2
        mb = 0.9 * mb + 0.1 * gb; vb = 0.999 * vb + 0.001 * gb ** 2
        W -= lr * (mW / (1 - 0.9 ** t)) / (np.sqrt(vW / (1 - 0.999 ** t)) + 1e-8)
        b -= lr * (mb / (1 - 0.9 ** t)) / (np.sqrt(vb / (1 - 0.999 ** t)) + 1e-8)
    return W, b

def predecir(X, W, b):
    z = X @ W + b
    z -= z.max(axis=1, keepdims=True)
    p = np.exp(z)
    return p / p.sum(axis=1, keepdims=True)

def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("carpeta", help="carpeta con los audios etiquetados")
    a.add_argument("--pliegues", type=int, default=5, help="pliegues de la validación cruzada")
    args = a.parse_args()
    base = Path(args.carpeta)
    if not base.is_dir():
        sys.exit(f"No existe la carpeta {base}")
    if m.red_neuronal() is None:
        sys.exit(f"La red neuronal no está disponible: {m.estado_red()}")

    archivos = [p for p in sorted(base.rglob("*")) if p.is_file() and p.suffix.lower() in m.EXTENSIONES_AUDIO]
    print(f"Leyendo {len(archivos)} audios de {base} ...")
    X, y_txt, grupo, sin_etiqueta = [], [], [], []
    t0 = time.time()
    for i, ruta in enumerate(archivos):
        cat = etiqueta_de(ruta, base)
        if not cat:
            sin_etiqueta.append(ruta.name)
            continue
        try:
            for h in huellas_de(ruta):
                X.append(h); y_txt.append(cat); grupo.append(i)
        except Exception as e:
            print(f"  (se omite {ruta.name}: {e})")
    if sin_etiqueta:
        print(f"  {len(sin_etiqueta)} audio(s) sin categoría reconocible (se omiten), p. ej.: {', '.join(sin_etiqueta[:5])}")
    clases = sorted(set(y_txt), key=m.CATEGORIAS.index)
    if len(clases) < 2:
        sys.exit("Se necesitan audios de al menos 2 categorías (idealmente incluye ambiente_natural).")
    X = np.array(X, np.float32); y = np.array([clases.index(c) for c in y_txt]); grupo = np.array(grupo)
    por_clase = collections.Counter(y_txt)
    archivos_por_clase = {c: len({g for g, t in zip(grupo, y_txt) if t == c}) for c in clases}
    print(f"{len(X)} trozos de {len(set(grupo))} audios en {time.time() - t0:.0f} s:")
    for c in clases:
        aviso = "   <- pocos: agrega más" if archivos_por_clase[c] < 10 else ""
        print(f"  {c:17} {archivos_por_clase[c]:4} audios ({por_clase[c]} trozos){aviso}")

    media, escala = X.mean(axis=0), X.std(axis=0) + 1e-3
    Xn = (X - media) / escala

    # Validación cruzada agrupada por archivo: los trozos de un mismo audio nunca están en entrenamiento y prueba a la vez
    rng = np.random.default_rng(0)
    ids = np.array(sorted(set(grupo)))
    rng.shuffle(ids)
    pliegues = max(2, min(args.pliegues, min(archivos_por_clase.values())))
    pred = np.zeros(len(X), int)
    for f in range(pliegues):
        prueba = np.isin(grupo, ids[f::pliegues])
        W, b = entrenar(Xn[~prueba], y[~prueba], len(clases))
        pred[prueba] = predecir(Xn[prueba], W, b).argmax(axis=1)
    acierto = float((pred == y).mean())
    print(f"\nValidación cruzada ({pliegues} pliegues, audios nunca vistos): precisión {acierto:.1%}")
    print("Matriz de confusión (filas = real, columnas = predicho):")
    print(" " * 18 + " ".join(f"{c[:8]:>8}" for c in clases))
    for i, c in enumerate(clases):
        print(f"  {c:16}" + " ".join(f"{int(((y == i) & (pred == j)).sum()):8}" for j in range(len(clases))))
    amen = [i for i, c in enumerate(clases) if c in m.CATEGORIAS_AMENAZA]
    no_amen = [i for i, c in enumerate(clases) if c not in m.CATEGORIAS_AMENAZA]
    if amen and no_amen:
        falsas = np.isin(y, no_amen) & np.isin(pred, amen)
        perdidas = np.isin(y, amen) & ~np.isin(pred, amen)
        print(f"Falsas alarmas: {falsas.sum() / np.isin(y, no_amen).sum():.1%} | "
              f"amenazas no detectadas: {perdidas.sum() / np.isin(y, amen).sum():.1%}")

    W, b = entrenar(Xn, y, len(clases))
    destino = RAIZ / "modelos" / "ajuste_local.npz"
    np.savez_compressed(destino, W=W, b=b, media=media.astype(np.float32), escala=escala.astype(np.float32),
                        clases=np.array(clases), precision_cv=np.float32(acierto), n_audios=np.int32(len(set(grupo))))
    print(f"\nModelo guardado en {destino}. Reinicia monitoreo.py para usarlo.")
    if acierto < 0.80:
        print("AVISO: precisión menor a 80 %. El sistema solo usará este modelo cuando esté muy seguro; "
              "agrega más audios (sobre todo de las categorías con errores) y vuelve a entrenar.")

if __name__ == "__main__":
    main()
