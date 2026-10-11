"""Pruebas de auditoria/reglas_mediacap.py — stdlib unittest, con fixtures sintéticos.

    python -m unittest tests.test_reglas_mediacap -v
"""
import contextlib
import hashlib
import importlib.util
import io
import tempfile
import textwrap
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "reglas_mediacap", Path(__file__).resolve().parents[1] / "auditoria" / "reglas_mediacap.py")
rm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rm)

DAEMON_OK = '''
def load_config_from_db():
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ms.slug, cc.stream_url, cc.hls_live_restart
            FROM capture_config cc JOIN media_sources ms ON true
        """)
        return [{"slug": r[0], "stream_url": r[1], "hls_live_restart": r[2]}
                for r in cur.fetchall()]

def spawn(cfg, old):
    url = cfg["stream_url"]
    if cfg.get("hls_live_restart", True):
        sc.append("--hls-live-restart")
    for p in [old.get("main"), old.get("aux")]:
        pass
    subprocess.Popen(["ffmpeg"], stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
'''


class Repo:
    def __init__(self, archivos):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name)
        for rel, txt in archivos.items():
            f = self.raiz / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(textwrap.dedent(txt), encoding="utf-8")

    def auditar(self):
        return rm.auditar(self.raiz)["hallazgos"]

    def huella(self):
        return {p.relative_to(self.raiz).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.raiz.rglob("*") if p.is_file()}

    def cerrar(self):
        self.tmp.cleanup()


def _reglas(hs, estado=None):
    return sorted(h["regla"] for h in hs if estado is None or h["estado"] == estado)


class TestReglas(unittest.TestCase):
    def auditar(self, archivos):
        r = Repo(archivos)
        self.addCleanup(r.cerrar)
        return r.auditar()

    def test_daemon_sano_sin_hallazgos(self):
        self.assertEqual(self.auditar({rm.DAEMON: DAEMON_OK}), [])

    def test_mc001_bandera_que_no_esta_en_el_select(self):
        txt = DAEMON_OK + '\ndef x(cfg):\n    return cfg.get("seg_max_retry", 0)\n'
        hs = self.auditar({rm.DAEMON: txt})
        self.assertEqual(_reglas(hs, "CONFIRMED"), ["MC001"])
        self.assertIn("seg_max_retry", hs[0]["titulo"])

    def test_mc001_ignora_el_dict_de_procesos(self):
        # old["main"]/old["aux"] son procesos, no columnas de capture_config.
        self.assertEqual(_reglas(self.auditar({rm.DAEMON: DAEMON_OK})), [])

    def test_mc002_select_y_dict_desalineados(self):
        txt = DAEMON_OK.replace("cc.stream_url, cc.hls_live_restart", "cc.stream_url")
        self.assertIn("MC002", _reglas(self.auditar({rm.DAEMON: txt}), "CONFIRMED"))

    def test_mc003_reconnect_at_eof_python_y_shell(self):
        hs = self.auditar({
            "scripts/a.py": 'A = ["-reconnect", "1", "-reconnect_at_eof", "1"]\n',
            "scripts/b.sh": "ffmpeg -reconnect_at_eof 1 -i \"$URL\"\n# -reconnect_at_eof 1 comentado\n",
            "scripts/ok.py": 'A = ["-reconnect_at_eof", "0"]\n',
        })
        self.assertEqual([(h["regla"], h["archivo"]) for h in hs],
                         [("MC003", "scripts/a.py"), ("MC003", "scripts/b.sh")])

    def test_mc004_mc005_popen_sin_grupo_y_con_pipe(self):
        hs = self.auditar({"scripts/p.py": textwrap.dedent('''
            subprocess.Popen(cmd, stderr=subprocess.PIPE)
            subprocess.Popen(cmd, start_new_session=True, stderr=subprocess.DEVNULL)
        ''')})
        self.assertEqual(_reglas(hs), ["MC004", "MC005"])
        self.assertTrue(all(h["estado"] == "POTENTIAL" for h in hs))

    def test_mc006_sql_en_bucle(self):
        hs = self.auditar({"scripts/s.py": textwrap.dedent('''
            def f(cur, xs):
                cur.execute("SELECT 1")
                for x in xs:
                    cur.execute("SELECT %s", (x,))
        ''')})
        self.assertEqual([(h["regla"], h["linea"]) for h in hs], [("MC006", 5)])

    def test_mc007_hls_live_restart_incondicional(self):
        txt = DAEMON_OK.replace('    if cfg.get("hls_live_restart", True):\n        sc.append',
                                '    sc.append')
        self.assertIn("MC007", _reglas(self.auditar({rm.DAEMON: txt}), "CONFIRMED"))

    def test_archivo_roto_no_tumba_la_auditoria(self):
        hs = self.auditar({"monitor/roto.py": "x = f'{🟢}'\n",
                           "scripts/a.py": 'A = ["-reconnect_at_eof", "1"]\n'})
        self.assertEqual(_reglas(hs), ["MC000", "MC003"])
        self.assertEqual(next(h for h in hs if h["regla"] == "MC000")["estado"], "POTENTIAL")

    def test_supresion_revisada_queda_como_falso_positivo(self):
        hs = self.auditar({"scripts/p.py":
                           "subprocess.Popen(cmd)  # auditoria: ok MC004 proceso corto, se espera\n"})
        self.assertEqual(hs[0]["estado"], "FALSE_POSITIVE")
        self.assertIn("proceso corto", hs[0]["evidencia"])

    def test_supresion_de_otra_regla_no_aplica(self):
        hs = self.auditar({"scripts/p.py": "subprocess.Popen(cmd)  # auditoria: ok MC005 x\n"})
        self.assertEqual(hs[0]["estado"], "POTENTIAL")

    def test_fuera_de_alcance_no_se_audita(self):
        self.assertEqual(self.auditar({"archive/viejo.py": 'A = ["-reconnect_at_eof", "1"]\n'}), [])

    def test_no_modifica_el_repo_auditado(self):
        r = Repo({rm.DAEMON: DAEMON_OK, "scripts/b.sh": "ffmpeg -reconnect_at_eof 1\n",
                  "monitor/roto.py": "def (\n"})
        self.addCleanup(r.cerrar)
        antes = r.huella()
        r.auditar()
        self.assertEqual(r.huella(), antes)

    def test_hallazgos_traen_evidencia_y_ubicacion_valida(self):
        r = Repo({"scripts/b.sh": "x\nffmpeg -reconnect_at_eof 1\n"})
        self.addCleanup(r.cerrar)
        for h in r.auditar():
            self.assertTrue(h["evidencia"] and h["como_confirmar"])
            lineas = (r.raiz / h["archivo"]).read_text().splitlines()
            self.assertIn("reconnect_at_eof", lineas[h["linea"] - 1])

    def test_codigo_de_salida(self):
        r = Repo({"scripts/p.py": "subprocess.Popen(cmd)\n"})
        self.addCleanup(r.cerrar)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rm.main(["--raiz", str(r.raiz)]), 0)      # solo POTENTIAL
            (r.raiz / "scripts/b.sh").write_text("ffmpeg -reconnect_at_eof 1\n")
            self.assertEqual(rm.main(["--raiz", str(r.raiz)]), 1)      # CONFIRMED bloquea


if __name__ == "__main__":
    unittest.main()
