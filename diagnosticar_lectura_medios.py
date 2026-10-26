"""Dos lecturas públicas. No importa el worker, boto3, dotenv ni Telegram."""
import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from mauricio_article_reader import read_public_article

BASE = Path(__file__).resolve().parent
CASES = {
    "Cambio": "https://cambiocolombia.com/poder/articulo/2026/10/secretaria-de-senado-aclara-que-el-vicio-de-tramite-del-presupuesto-ocurrio-en-la-camara-el-articulo-65-fue-aprobado-sin-proposiciones",
    "El País": "https://elpais.com/america-colombia/2026-10-22/abelardo-de-la-espriella-nombra-a-alejandro-ordonez-en-reemplazo-de-rodrigo-lara-como-ministro-del-interior.html",
}


def load_identity():
    # Extraer únicamente funciones puras y regex. Nunca ejecutar el módulo completo.
    tree = ast.parse((BASE / "google_news_rss_only_v16_lectura.py").read_text(encoding="utf-8"))
    names = {"safe_text", "mauricio_person_evidence", "MAURICIO_FULL_NAME_RE",
             "MAURICIO_POLITICAL_CONTEXT_RE", "MAURICIO_OTHER_PERSON_RE"}
    nodes = [node for node in tree.body
             if (isinstance(node, ast.FunctionDef) and node.name in names)
             or (isinstance(node, ast.Assign) and any(
                 isinstance(t, ast.Name) and t.id in names for t in node.targets))]
    namespace = {"re": re, "urlparse": urlparse}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "identidad_pura", "exec"), namespace)
    return namespace["mauricio_person_evidence"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="diagnostico_lectura_medios.json")
    parser.add_argument("--probar-respaldo-cambio", action="store_true",
                        help="Si falla Cambio, probar Jina Reader enviando únicamente su URL pública; sin clave API.")
    args = parser.parse_args()
    identity = load_identity()
    report = {"generado_utc": datetime.now(timezone.utc).isoformat(),
              "solo_lectura": True, "s3": False, "telegram": False, "medios": []}
    for medium, url in CASES.items():
        result = read_public_article(url, allow_cambio_fallback=args.probar_respaldo_cambio)
        confirmed, evidence = identity({"url_original": url, "texto_articulo": result.text})
        row = {"medio": medium, "url_original": url, "estado": result.status,
               "version": result.version, "caracteres_cuerpo": len(result.text),
               "identidad_confirmada": confirmed, "evidencia": evidence,
               "intentos": result.attempts}
        report["medios"].append(row)
        print(f"{medium} | lectura={result.status} | identidad={'CONFIRMADA' if confirmed else 'NO CONFIRMADA'} | version={result.version or '-'} | caracteres={len(result.text)}", flush=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"INFORME: {args.output}")
    print("S3=NO | Telegram=NO | cambios de producción=NO")


if __name__ == "__main__":
    main()
