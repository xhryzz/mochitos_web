# build_precompress_static.py
"""
Pre-comprime ficheros estáticos a .gz y .br para que WhiteNoise los sirva al vuelo.
Uso:
    python build_precompress_static.py [ruta_static]
"""
import os, sys, gzip, brotli

def compress_file(path: str):
    # Evita recomprimir si ya existen
    gz_path = path + ".gz"
    br_path = path + ".br"

    # Lee original
    with open(path, "rb") as f:
        data = f.read()

    # Gzip
    if not os.path.exists(gz_path):
        with gzip.open(gz_path, "wb", compresslevel=6) as f:
            f.write(data)

    # Brotli
    if not os.path.exists(br_path):
        comp = brotli.compress(data, quality=5)
        with open(br_path, "wb") as f:
            f.write(comp)

def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "static"
    if not os.path.isdir(root):
        print(f"[WARN] No existe carpeta: {root}")
        return

    exts = {".css", ".js", ".svg", ".json", ".xml", ".txt", ".webmanifest", ".ico"}
    count = 0
    for base, _, files in os.walk(root):
        for name in files:
            if any(name.endswith(ext) for ext in exts):
                path = os.path.join(base, name)
                try:
                    compress_file(path)
                    count += 1
                except Exception as e:
                    print("[ERR]", path, e)

    print(f"Precomprimidos {count} archivos en {root}")

if __name__ == "__main__":
    main()
