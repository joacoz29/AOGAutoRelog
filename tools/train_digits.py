"""Genera templates/exp_digits.npz a partir de capturas del HUD con su texto real.

Uso:
    python tools/train_digits.py etiquetas.json            # reemplaza los prototipos
    python tools/train_digits.py etiquetas.json --append   # los suma a los que ya hay (por ejemplo, para otro estilo de texto)

etiquetas.json:  {"capturas/f00.png": {"exp": "97.26", "hp": "3307/3478", "mp": "3242/3384"}, ...}
Las capturas son del area cliente del juego a 1366x768 (autorelog.grab). Cada linea del HUD usa la misma
tipografia, asi que HP y MP sirven para aprender digitos que el porcentaje de exp todavia no mostro.
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import expmon  # noqa: E402

BANDS = {"exp": expmon.BAND, "hp": (34, 41, 138, 226), "mp": (48, 55, 138, 226)}


def main(path):
    labels_file = Path(path)
    data = json.loads(labels_file.read_text(encoding="utf-8"))
    labels, masks, bad = [], [], []
    for img_path, lines in data.items():
        img = cv2.imread(str(labels_file.parent / img_path))
        if img is None:
            bad.append((img_path, "no se pudo leer"))
            continue
        for band, text in lines.items():
            gl = expmon.glyphs(img, BANDS[band])
            if band == "exp":
                gl = gl[:len(text)]
            if len(gl) != len(text):
                bad.append((img_path, band, f"{len(gl)} caracteres vs {len(text)} esperados"))
                continue
            for m, ch in zip(gl, text):
                if ch.isdigit():
                    labels.append(ch)
                    masks.append(expmon.pad(m))
    if bad:
        print("omitidos:", bad)
    out = expmon.DIGITS
    if "--append" in sys.argv and out.exists():
        old = np.load(out)
        labels = [str(x) for x in old["labels"]] + labels
        masks = list(old["masks"]) + masks
    np.savez_compressed(out, labels=np.array(labels), masks=np.array(masks, dtype=np.uint8))
    cnt = {d: labels.count(d) for d in "0123456789"}
    print(f"guardado {out} con {len(labels)} ejemplos: {cnt}")
    falta = [d for d, n in cnt.items() if n == 0]
    if falta:
        print("ATENCION: faltan ejemplos de los digitos", falta)


if __name__ == "__main__":
    main(sys.argv[1])
