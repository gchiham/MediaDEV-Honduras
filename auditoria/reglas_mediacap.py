#!/usr/bin/env python3
"""Reglas de auditoría específicas de mediaCAP — solo lectura, solo stdlib, sin red ni DB.

Cada regla sale de un incidente real (ver .claude/skills/mediacap-ffmpeg/references/incidentes.md).
Analiza el código con `ast`; nunca importa ni ejecuta los módulos auditados.

    python auditoria/reglas_mediacap.py [--raiz .] [--json salida.json] [--md salida.md]

Salida: 1 si hay algún hallazgo CONFIRMED (bloquea CI), 0 si no.

Suprimir un hallazgo revisado: en la línea señalada, `# auditoria: ok MC004 <razón>`.
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

# Qué se audita (rutas relativas a la raíz del repo). archive/ es código muerto a propósito.
ALCANCE = ["daemon", "scripts", "monitor"]
DAEMON = "daemon/stream_daemon.py"
# Variables que en el daemon contienen una fila de capture_config. No incluir `old`: en
# spawn_stream es el dict de procesos ({"main", "aux"}), no una config.
NOMBRES_CFG = {"cfg", "c"}

_SUPRIMIR = re.compile(r"#\s*auditoria:\s*ok\s+(MC\d{3})")


def hallazgo(regla, estado, sev, archivo, linea, titulo, evidencia, confirmar):
    return {"regla": regla, "estado": estado, "severidad": sev, "archivo": archivo,
            "linea": linea, "titulo": titulo, "evidencia": evidencia, "como_confirmar": confirmar}


def _str(nodo):
    return nodo.value if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) else None


def _es_llamada(nodo, attr):
    return isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == attr


def _kw(llamada, nombre):
    return next((k.value for k in llamada.keywords if k.arg == nombre), None)


def _padres(arbol):
    p = {}
    for n in ast.walk(arbol):
        for h in ast.iter_child_nodes(n):
            p[h] = n
    return p


# ── MC001 / MC002 — capture_config ↔ load_config_from_db ─────────────────────
# Incidente: una bandera nueva (hls_live_restart, ffmpeg_extra) que existe como columna pero
# no está en el SELECT hace que el daemon caiga al default en silencio.

def _sql_select_cols(sql):
    m = re.search(r"SELECT(.*?)\bFROM\b", sql, re.S | re.I)
    return [c.strip() for c in m.group(1).split(",") if c.strip()] if m else []


def regla_capture_config(arbol, archivo):
    out = []
    fn = next((n for n in ast.walk(arbol)
               if isinstance(n, ast.FunctionDef) and n.name == "load_config_from_db"), None)
    if fn is None:
        return [hallazgo("MC002", "POTENTIAL", "P2", archivo, 1,
                         "No se encontró load_config_from_db()",
                         "La regla de capture_config no pudo verificarse.",
                         "Revisar si la carga de config cambió de nombre y actualizar la regla.")]

    cols, claves, idx_max, linea = [], set(), -1, fn.lineno
    for n in ast.walk(fn):
        if _es_llamada(n, "execute") and n.args and _str(n.args[0]):
            cols, linea = _sql_select_cols(_str(n.args[0])), n.lineno
        if isinstance(n, ast.Dict):
            claves |= {_str(k) for k in n.keys if _str(k)}
            for v in n.values:
                if isinstance(v, ast.Subscript) and isinstance(v.slice, ast.Constant) \
                        and isinstance(v.slice.value, int):
                    idx_max = max(idx_max, v.slice.value)

    if cols and idx_max + 1 != len(cols):
        out.append(hallazgo(
            "MC002", "CONFIRMED", "P1", archivo, linea,
            "El SELECT de capture_config y el mapeo r[i] no cuadran",
            f"SELECT trae {len(cols)} columnas ({', '.join(cols)}) pero el dict usa hasta r[{idx_max}].",
            "Alinear el SELECT con los índices del dict en load_config_from_db()."))

    for n in ast.walk(arbol):
        clave, ln = None, getattr(n, "lineno", 0)
        if _es_llamada(n, "get") and isinstance(n.func.value, ast.Name) \
                and n.func.value.id in NOMBRES_CFG and n.args:
            clave = _str(n.args[0])
        elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
                and n.value.id in NOMBRES_CFG and isinstance(n.ctx, ast.Load):
            clave = _str(n.slice)
        if clave and claves and clave not in claves:
            out.append(hallazgo(
                "MC001", "CONFIRMED", "P1", archivo, ln,
                f"El daemon lee '{clave}' pero load_config_from_db() no la carga",
                f"Claves cargadas: {sorted(claves)}. La lectura de '{clave}' siempre dará el default.",
                f"Agregar cc.{clave} al SELECT y al dict, o confirmar que no es columna de capture_config."))
    return out


# ── MC003 — reconnect_at_eof=1 ───────────────────────────────────────────────
# Incidente Teleceiba: en HLS de ventana corta, reconnect_at_eof 1 re-pide el playlist en bucle.

def regla_reconnect_at_eof(arbol, archivo):
    out = []
    for n in ast.walk(arbol):
        if isinstance(n, ast.List):
            vals = [_str(e) for e in n.elts]
            for i, v in enumerate(vals[:-1]):
                if v == "-reconnect_at_eof" and vals[i + 1] not in ("0", None):
                    out.append(hallazgo(
                        "MC003", "CONFIRMED", "P1", archivo, n.lineno,
                        f"ffmpeg con -reconnect_at_eof {vals[i + 1]}",
                        "En HLS de ventana corta (Teleceiba) provocó que dejara de grabar.",
                        "Usar -reconnect_at_eof 0; si una radio Icecast lo necesita, hacerlo por estación."))
    return out


# ── MC004 / MC005 — procesos hijos ───────────────────────────────────────────
# Incidente load 96: ffmpeg que no están en su propio grupo quedan huérfanos (PPID=1) al
# reiniciar. Y stderr=PIPE que nadie lee llena el buffer de 64 KB y congela a ffmpeg.

def regla_procesos(arbol, archivo):
    out = []
    for n in ast.walk(arbol):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "Popen"):
            continue
        pre, sns = _kw(n, "preexec_fn"), _kw(n, "start_new_session")
        con_grupo = (isinstance(pre, ast.Attribute) and pre.attr == "setsid") or \
                    (isinstance(sns, ast.Constant) and sns.value is True)
        if not con_grupo:
            out.append(hallazgo(
                "MC004", "POTENTIAL", "P2", archivo, n.lineno,
                "Popen sin grupo de procesos propio",
                "Sin preexec_fn=os.setsid / start_new_session=True no se puede matar el árbol con "
                "killpg; en el incidente de load 96 eso dejó ffmpeg huérfanos acumulándose.",
                "Ver si el proceso es de larga vida; si lo es, lanzarlo con start_new_session=True."))
        err = _kw(n, "stderr")
        if isinstance(err, ast.Attribute) and err.attr == "PIPE":
            out.append(hallazgo(
                "MC005", "POTENTIAL", "P2", archivo, n.lineno,
                "Popen con stderr=PIPE",
                "Si nadie lee el pipe, al llenarse (64 KB) el hijo se bloquea en silencio.",
                "Confirmar que stderr se consume (communicate/hilo) o mandarlo a archivo/DEVNULL."))
    return out


# ── MC006 — SQL dentro de un bucle ───────────────────────────────────────────
# Principio 5 de CLAUDE.md: batch con GROUP BY, nunca loops por stream (2 vCPU).

def regla_sql_en_bucle(arbol, archivo):
    out, padres = [], _padres(arbol)
    for n in ast.walk(arbol):
        if not _es_llamada(n, "execute"):
            continue
        p = padres.get(n)
        while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(p, (ast.For, ast.While, ast.comprehension)):
                out.append(hallazgo(
                    "MC006", "POTENTIAL", "P3", archivo, n.lineno,
                    "execute() dentro de un bucle",
                    "Una consulta por iteración; con ~20 estaciones multiplica la carga sobre PG.",
                    "Ver cuántas iteraciones hace en prod; si es por estación, pasar a batch/executemany."))
                break
            p = padres.get(p)
    return out


# ── MC007 — --hls-live-restart incondicional ─────────────────────────────────
# Incidente canal_10: con DVR de horas (Dailymotion) capturaba a 4x con duplicados.

def regla_hls_live_restart(arbol, archivo):
    out, padres = [], _padres(arbol)
    for n in ast.walk(arbol):
        if _str(n) != "--hls-live-restart":
            continue
        p = padres.get(n)
        while p is not None and not isinstance(p, (ast.If, ast.IfExp, ast.FunctionDef, ast.Module)):
            p = padres.get(p)
        if not isinstance(p, (ast.If, ast.IfExp)):
            out.append(hallazgo(
                "MC007", "CONFIRMED", "P1", archivo, n.lineno,
                "--hls-live-restart aplicado sin condición por estación",
                "En canal_10 (DVR de horas) produjo archivos de 1 h con 4 h de contenido duplicado.",
                "Condicionarlo a capture_config.hls_live_restart."))
    return out


REGLAS_PY = [regla_reconnect_at_eof, regla_procesos, regla_sql_en_bucle, regla_hls_live_restart]


def regla_shell(texto, archivo):
    out = []
    for i, linea in enumerate(texto.splitlines(), 1):
        m = re.search(r"-reconnect_at_eof\s+[\"']?([1-9])", linea)
        if m and not linea.lstrip().startswith("#"):
            out.append(hallazgo(
                "MC003", "CONFIRMED", "P1", archivo, i,
                f"ffmpeg con -reconnect_at_eof {m.group(1)}",
                "En HLS de ventana corta (Teleceiba) provocó que dejara de grabar.",
                "Usar -reconnect_at_eof 0."))
    return out


def auditar(raiz: Path):
    hallazgos, revisados = [], []
    archivos = sorted(f for d in ALCANCE if (raiz / d).is_dir()
                      for f in (raiz / d).rglob("*") if f.suffix in (".py", ".sh"))
    for f in archivos:
        rel = f.relative_to(raiz).as_posix()
        texto = f.read_text(encoding="utf-8", errors="replace")
        revisados.append(rel)
        if f.suffix == ".sh":
            res = regla_shell(texto, rel)
        else:
            try:
                arbol = ast.parse(texto, filename=rel)
            except SyntaxError as e:
                hallazgos.append(hallazgo(
                    "MC000", "POTENTIAL", "P3", rel, e.lineno or 1,
                    "El archivo no es Python válido",
                    f"{e.msg}. Falla al importarlo o ejecutarlo.",
                    "Buscar si algún unit systemd, cron o import lo usa; si no, moverlo a archive/."))
                continue
            res = [h for r in REGLAS_PY for h in r(arbol, rel)]
            if rel == DAEMON:
                res += regla_capture_config(arbol, rel)
        lineas = texto.splitlines()
        for h in res:
            l = lineas[h["linea"] - 1] if 0 < h["linea"] <= len(lineas) else ""
            m = _SUPRIMIR.search(l)
            if m and m.group(1) == h["regla"]:
                h["estado"] = "FALSE_POSITIVE"
                h["evidencia"] += f"  [Suprimido en el código: {l.split('auditoria:', 1)[1].strip()}]"
        hallazgos += res
    orden = {"CONFIRMED": 0, "LIKELY": 1, "POTENTIAL": 2, "FALSE_POSITIVE": 3}
    hallazgos.sort(key=lambda h: (orden[h["estado"]], h["severidad"], h["archivo"], h["linea"]))
    return {"alcance": ALCANCE, "archivos_revisados": len(revisados), "hallazgos": hallazgos}


def _loc(h):
    return f"{h['archivo']}:{h['linea']}" if h["linea"] else h["archivo"]


def a_markdown(rep):
    from collections import Counter
    cnt = Counter(h["estado"] for h in rep["hallazgos"])
    out = ["## Reglas de mediaCAP", "",
           f"Archivos revisados: {rep['archivos_revisados']} ({', '.join(rep['alcance'])}). "
           + " · ".join(f"**{k}**: {v}" for k, v in sorted(cnt.items())) if cnt else
           f"Archivos revisados: {rep['archivos_revisados']}. Sin hallazgos.", ""]
    if rep["hallazgos"]:
        out += ["| Estado | Sev. | Regla | Ubicación | Hallazgo |", "|---|---|---|---|---|"]
        for h in rep["hallazgos"]:
            out.append(f"| {h['estado']} | {h['severidad']} | {h['regla']} | "
                       f"`{_loc(h)}` | {h['titulo']} |")
        out += ["", "<details><summary>Evidencia y cómo confirmar</summary>", ""]
        for h in rep["hallazgos"]:
            out += [f"- **{h['regla']} `{_loc(h)}`** — {h['evidencia']} "
                    f"*Confirmar:* {h['como_confirmar']}"]
        out += ["", "</details>"]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raiz", default=".")
    ap.add_argument("--json")
    ap.add_argument("--md")
    a = ap.parse_args(argv)
    rep = auditar(Path(a.raiz))
    md = a_markdown(rep)
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    if a.md:
        Path(a.md).write_text(md, encoding="utf-8")
    sys.stdout.write(md)
    return 1 if any(h["estado"] == "CONFIRMED" for h in rep["hallazgos"]) else 0


if __name__ == "__main__":
    sys.exit(main())
