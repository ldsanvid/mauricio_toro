"""Prueba segura de los cambios: no hace conexiones de red ni escribe S3/Telegram."""
import ast
import hashlib
from pathlib import Path
import re
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent


def _pure_functions(path, names, constant_names=()):
    source = ast.parse(path.read_text(encoding="utf-8"))
    declarations = []
    for item in source.body:
        if isinstance(item, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in constant_names
            for t in item.targets
        ):
            declarations.append(item)
        if isinstance(item, ast.FunctionDef) and item.name in names:
            declarations.append(item)
    if len([d for d in declarations if isinstance(d, ast.FunctionDef)]) != len(names):
        raise RuntimeError(f"No se encontraron todas las funciones en {path.name}")
    scope = {
        "re": re, "hashlib": hashlib, "urlparse": urlparse,
        "safe_text": lambda v: "" if v is None else str(v).strip(),
    }
    code = compile(ast.Module(body=declarations, type_ignores=[]), str(path), "exec")
    exec(code, scope)
    return scope


def main():
    worker = _pure_functions(
        ROOT / "google_news_worker_mauricio_v15_onecycle_identidad_v5.py",
        ("mauricio_person_evidence", "article_publication_key", "telegram_guard_key"),
        ("MAURICIO_FULL_NAME_RE", "MAURICIO_POLITICAL_CONTEXT_RE", "MAURICIO_OTHER_PERSON_RE"),
    )
    brief = _pure_functions(
        ROOT / "news_brief_mauricio_ejes_v18.py",
        ("topic_supported_by_content",),
    )
    is_mauricio = worker["mauricio_person_evidence"]
    guard = worker["telegram_guard_key"]
    supports = brief["topic_supported_by_content"]

    fixtures = [
        ("mención Semana", {
            "titulo": "Qué cambia para taxistas y conductores de apps",
            "texto_articulo": "Daniel Briceño, Mauricio Toro, Cathy Juvinao y María Lucía Villalba lideraron el proyecto de ley.",
            "url_original": "https://www.semana.com/vehiculos/articulo/ejemplo/202620/",
        }, True),
        ("mención El Espectador", {
            "titulo": "Congreso quiere regular IA y plataformas",
            "texto_articulo": "Los congresistas que firmaron el proyecto son Mauricio Toro (Alianza Verde) y otros.",
            "url_original": "https://www.elespectador.com/politica/congreso-ejemplo/",
        }, True),
        ("crédito fotográfico chileno", {
            "titulo": "Universidad Católica vs La Calera",
            "texto_articulo": "Copa Chile 2026. (Foto: Mauricio Toro/Agencia Uno). Se disputa el partido.",
            "url_original": "https://www.prensafutbol.cl/ejemplo/",
        }, False),
        ("director de Sernageomin", {
            "titulo": "Informe de aluviones en Chile",
            "texto_articulo": "El director nacional de Sernageomin, Mauricio Toro, aclaró el riesgo de aluviones.",
            "url_original": "https://www.cooperativa.cl/noticias/ejemplo/",
        }, False),
        ("Óscar Mauricio Toro", {
            "titulo": "CNE falla a favor de Óscar Mauricio Toro",
            "texto_articulo": "En Dosquebradas, Óscar Mauricio Toro Valencia.",
            "url_original": "https://www.eje21.com.co/ejemplo/",
        }, False),
        ("Mauricio Toro-Goya", {
            "titulo": "Mauricio Toro-Goya, artista",
            "texto_articulo": "Obra de Mauricio Toro-Goya",
            "url_original": "https://artishockrevista.com/ejemplo/",
        }, False),
        ("nota genérica de plataformas", {
            "titulo": "Fracking, aborto y plataformas vuelven al Congreso",
            "texto_articulo": "Un debate sobre plataformas y proyectos de ley, sin citar al congresista.",
            "url_original": "https://estrelladigital.com.co/politica/ejemplo/",
        }, False),
        ("mención explícita en titular", {
            "titulo": "Menos trámites, la apuesta de Mauricio Toro",
            "url_original": "https://confidencialnoticias.com/ejemplo/",
        }, True),
        ("nombre completo", {
            "titulo": "Nueva iniciativa",
            "texto_articulo": "Mauricio Andrés Toro Orjuela explicó la iniciativa.",
            "url_original": "https://ejemplo.com/noticia/",
        }, True),
    ]
    count = 0
    for name, article, expected in fixtures:
        actual, reason = is_mauricio(article)
        assert actual is expected, f"{name}: esperado={expected}, obtenido={actual}, razon={reason}"
        count += 1
        print(f"OK | {name}: {'alertar' if actual else 'descartar'} ({reason})")

    a = {"url_original": "https://www.semana.com/vehiculos/articulo/ejemplo/202620/", "story_key": "version-1"}
    b = {"url_original": "https://www.semana.com/vehiculos/articulo/ejemplo/202620/?outputType=amp", "story_key": "version-2"}
    c = {"url_original": "https://www.pulzo.com/amp/nacion/ejemplo-PP5328771", "story_key": "version-3"}
    assert guard(a) == guard(b), "Misma URL no deduplicada"
    assert guard(a) != guard(c), "Dos medios distintos se bloquearon"
    count += 2

    assert not supports("Educación", "Congreso aprueba el presupuesto nacional", "")
    assert supports("Educación", "SENA ofrece becas para estudiantes", "")
    assert not supports("Diversidad", "Se retiran obras del Museo Nacional", "")
    assert supports("Diversidad", "Retiran obras sobre historia LGBTIQ+ del Museo Nacional", "")
    assert not supports(
        "Alcaldía de Bogotá", "Congreso aprueba presupuesto nacional 2027", "",
        "tema_bogota_presupuesto_2027"
    )
    count += 5
    print(f"\n✅ {count} verificaciones correctas; sin S3 ni Telegram.")


if __name__ == "__main__":
    main()
