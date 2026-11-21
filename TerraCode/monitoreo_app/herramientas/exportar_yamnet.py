"""
Convierte el modelo YAMNet de Google (formato TFLite int8, licencia Apache 2.0) al archivo
modelos/yamnet.npz que usa clasificador_sonidos.py, para no depender de TensorFlow en el equipo.

Solo hace falta ejecutarlo si se quiere regenerar el .npz (el programa ya lo trae):
    pip install tflite
    python herramientas/exportar_yamnet.py ruta/lite-model_yamnet_classification_tflite_1.tflite

Modelo original: https://storage.googleapis.com/download.tensorflow.org/models/tflite/task_library/
                 audio_classification/android/lite-model_yamnet_classification_tflite_1.tflite
"""
import sys
from pathlib import Path
import numpy as np
import tflite

RAIZ = Path(__file__).resolve().parent.parent

def main(ruta_tflite):
    buf = Path(ruta_tflite).read_bytes()
    m = tflite.Model.GetRootAsModel(buf, 0)
    g = m.Subgraphs(0)
    ops = {v: k for k, v in tflite.BuiltinOperator.__dict__.items() if not k.startswith("_")}
    tipos = {tflite.TensorType.INT8: np.int8, tflite.TensorType.INT32: np.int32, tflite.TensorType.FLOAT32: np.float32}

    def tensor(i):
        t = g.Tensors(i)
        datos = m.Buffers(t.Buffer()).DataAsNumpy()
        if isinstance(datos, int) or datos is None:
            return None
        return np.frombuffer(datos.tobytes(), dtype=tipos[t.Type()]).reshape(t.ShapeAsNumpy())

    def escalas(i):
        q = g.Tensors(i).Quantization()
        return q.ScaleAsNumpy().astype(np.float32)

    salida = {}
    capas = []
    for k in range(g.OperatorsLength()):
        op = g.Operators(k)
        oc = m.OperatorCodes(op.OpcodeIndex())
        nombre = ops[max(oc.BuiltinCode(), oc.DeprecatedBuiltinCode())]
        e = op.InputsAsNumpy()
        if nombre == "MUL":
            salida["ventana"] = tensor(e[1]).astype(np.float32)
        elif nombre == "FULLY_CONNECTED" and "fc_w" not in salida and g.Tensors(e[0]).Type() == tflite.TensorType.FLOAT32:
            salida["mel"] = tensor(e[1]).astype(np.float32)          # [64, 257]
            salida["mel_suma"] = tensor(e[2]).astype(np.float32)     # [64]
        elif nombre in ("CONV_2D", "DEPTHWISE_CONV_2D"):
            o = (tflite.Conv2DOptions if nombre == "CONV_2D" else tflite.DepthwiseConv2DOptions)()
            t = op.BuiltinOptions(); o.Init(t.Bytes, t.Pos)
            i = len(capas)
            salida[f"w{i}"] = tensor(e[1])                                # pesos int8 originales
            salida[f"s{i}"] = escalas(e[1]).reshape((-1, 1, 1, 1) if nombre == "CONV_2D" else (1, 1, 1, -1))
            b = tensor(e[2]).astype(np.float32) * escalas(e[2])
            salida[f"b{i}"] = b
            capas.append((0 if nombre == "CONV_2D" else 1, o.StrideH()))
        elif nombre == "FULLY_CONNECTED":
            salida["fc_w"] = tensor(e[1])                               # int8 [521, 1024]
            salida["fc_s"] = escalas(e[1])
            salida["fc_b"] = tensor(e[2]).astype(np.float32) * escalas(e[2])
    salida["capas"] = np.array(capas, dtype=np.int32)
    destino = RAIZ / "modelos" / "yamnet.npz"
    np.savez_compressed(destino, **salida)
    print(f"Guardado {destino} ({destino.stat().st_size / 1e6:.1f} MB, {len(capas)} capas convolucionales)")

if __name__ == "__main__":
    main(sys.argv[1])
