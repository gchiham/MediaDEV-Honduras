"""Pruebas de auditoria/recolector/mediacap_inventario.py — stdlib unittest, sin tocar el sistema.

    python -m unittest tests.test_inventario_mediacap -v

Todo corre contra árboles temporales y un `run` falso; ninguna prueba ejecuta systemctl, ps ni du reales.
"""
import builtins
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
DIR_REC = RAIZ / "auditoria" / "recolector"
_SPEC = importlib.util.spec_from_file_location("mediacap_inventario", DIR_REC / "mediacap_inventario.py")
inv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inv)

# Secretos FALSOS armados por partes: así ningún escáner (gitleaks en CI) los ve como literales.
TOKEN_TG = "5955525376:AAH" + "x" * 32
SECRETO = "ClaveSuperSecreta-123"
AWS_FALSA = "AKIA" + "ABCDEFGHIJKLMNOP"
PEM_INI, PEM_FIN = "-----BEGIN " + "OPENSSH PRIVATE KEY-----", "-----END " + "OPENSSH PRIVATE KEY-----"
PEM_PKCS8 = "-----BEGIN " + "PRIVATE KEY-----"


def huella(raiz: Path) -> dict:
    return {p.relative_to(raiz).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in raiz.rglob("*") if p.is_file()}


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def escribir(self, rel, texto):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(texto, encoding="utf-8")
        return p


class Redaccion(unittest.TestCase):
    def test_quita_secretos_conocidos(self):
        casos = [
            f"TOKEN={TOKEN_TG}",
            f"AWS_ACCESS_KEY_ID={AWS_FALSA}",
            f"Environment=DB_PASSWORD={SECRETO}",
            "Environment=\"OPENAI_API_KEY=" + "sk-" + f"proj-{SECRETO}\"",
            f"curl http://user:{SECRETO}@iptv.example.net/x.ts",
            f"http://tvprem.example:8080/live/usuario/{SECRETO}/2700.ts",
            f"https://cdn.example.com/a.m3u8?token={SECRETO}&b=1",
            "Authorization: " + f"Bearer {SECRETO}",
            f"{PEM_INI}\n{SECRETO}\n{PEM_FIN}",
        ]
        for c in casos:
            r = inv.redactar(c)
            self.assertNotIn(SECRETO, r, c)
            self.assertNotIn(TOKEN_TG, r, c)
            self.assertNotIn(AWS_FALSA, r, c)

    def test_no_destruye_lo_util(self):
        linea = "ExecStart=/usr/bin/python3 -I /opt/media-ai/daemon/stream_daemon.py --interval 10"
        self.assertEqual(inv.redactar(linea), linea)
        self.assertEqual(inv.redactar("0 */2 * * * root find /var/www/streams -mmin +120 -delete"),
                         "0 */2 * * * root find /var/www/streams -mmin +120 -delete")


class SecretosSoloMetadatos(Tmp):
    def test_nunca_abre_archivos_de_secretos(self):
        env = self.escribir("etc/mediadev-db.env", f"DB_PASSWORD={SECRETO}\n")
        pem = self.escribir("etc/mediadev/cf_private.pem", f"{PEM_PKCS8}{SECRETO}")
        self.escribir("etc/hostname", "mediacap\n")
        abiertos, abrir = [], builtins.open
        def espia(f, *a, **k):
            abiertos.append(str(f))
            return abrir(f, *a, **k)
        with mock.patch.object(builtins, "open", espia):
            out = inv.secretos_meta([(self.tmp / "etc", True), (self.tmp / "etc" / "mediadev", False)])
        self.assertNotIn(str(env), abiertos)
        self.assertNotIn(str(pem), abiertos)
        self.assertNotIn(SECRETO, json.dumps(out))
        rutas = {o["ruta"] for o in out}
        self.assertEqual(rutas, {str(env), str(pem)})              # hostname no es sensible: no se lista
        self.assertTrue(all({"dueno", "modo", "bytes", "mtime_utc"} <= set(o) for o in out))

    def test_codigo_sin_git_saltea_secretos_venv_y_respeta_limites(self):
        d = self.tmp / "destroyer"
        self.escribir("destroyer/cap/gateway/api.py", "print('x')\n")
        self.escribir("destroyer/cap/.env", f"X={SECRETO}")
        self.escribir("destroyer/llave.pem", SECRETO)
        self.escribir("destroyer/venv/lib/x.py", "x")
        self.escribir("destroyer/a/b/c/d/e/profundo.py", "x")
        self.escribir("destroyer/datos.bin", "x")
        res = inv.arbol_hashes([d])[0]
        rutas = [a["ruta"] for a in res["archivos"]]
        self.assertEqual(rutas, ["cap/gateway/api.py"])
        self.assertEqual(res["archivos"][0]["sha256"],
                         hashlib.sha256((d / "cap/gateway/api.py").read_bytes()).hexdigest())
        with mock.patch.object(inv, "MAX_ARCHIVOS", 0):
            self.assertTrue(inv.arbol_hashes([d])[0]["truncado"])


class Secciones(Tmp):
    def test_cron_y_units_redactados(self):
        self.escribir("cron.d/tvprem-watch", f"*/5 * * * * root TG_TOKEN={TOKEN_TG} /opt/x.py\n")
        out = inv.cron([self.tmp / "cron.d", self.tmp / "no-existe"])
        self.assertEqual(len(out), 1)
        self.assertNotIn(TOKEN_TG, out[0]["texto"])
        self.assertIn("/opt/x.py", out[0]["texto"])

    def test_systemd_con_run_falso(self):
        unit = self.escribir("stream-daemon.service",
                             f"[Service]\nEnvironment=TG_TOKEN={TOKEN_TG}\nExecStart=/usr/bin/python3 d.py\n")
        def run(cmd, timeout=20):
            if cmd[1] == "list-unit-files":
                return 0, "stream-daemon.service enabled enabled\nwg-quick@.service disabled enabled\n"
            if cmd[1] == "list-units":
                return 0, "stream-daemon.service loaded active running X\n"
            return 0, f"ActiveState=active\nNRestarts=0\nFragmentPath={unit}\nDropInPaths=\n"
        out = inv.systemd(run)
        self.assertEqual([u["unidad"] for u in out], ["stream-daemon.service"])   # plantillas @. fuera
        self.assertEqual(out[0]["ActiveState"], "active")
        self.assertNotIn(TOKEN_TG, json.dumps(out))
        self.assertIn("ExecStart", out[0]["archivos"][0]["texto"])

    def test_procesos_sin_argumentos_y_huerfanos(self):
        ps = "  10     1  50000 ffmpeg\n  11   900  40000 ffmpeg\n 900     1 30000 python3\nbasura\n"
        out = inv.procesos(lambda cmd, timeout=20: (0, ps))
        self.assertEqual((out["ffmpeg"], out["ffmpeg_ppid1"]), (2, 1))
        self.assertEqual(list(out["top_rss"]), ["ffmpeg", "python3"])

    def test_git_nunca_toma_locks(self):
        vistos = []
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        inv.git([self.tmp / "repo"], lambda cmd, timeout=20: (vistos.append(cmd), (0, ""))[1])
        self.assertTrue(vistos and all("--no-optional-locks" in c for c in vistos))
        self.assertFalse(any(x in c for c in vistos for x in ("add", "commit", "checkout", "reset", "pull")))

    @unittest.skipUnless(shutil.which("git"), "requiere git")
    def test_git_real_no_modifica_el_repo(self):
        r = self.tmp / "repo"
        r.mkdir()
        g = ["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(g + ["init", "-q"], check=True)
        (r / "a.py").write_text("x = 1\n")
        subprocess.run(g + ["add", "a.py"], check=True)
        subprocess.run(g + ["commit", "-qm", "x"], check=True)
        (r / "a.py").write_text("x = 2\n")                    # cambio sin commitear = drift
        antes = huella(r)
        out = inv.git([r])
        self.assertEqual(huella(r), antes)
        self.assertEqual(out[0]["cambios"], [" M a.py"])
        self.assertRegex(out[0]["head"], r"^[0-9a-f]{40}$")


class RecolectarYEscribir(Tmp):
    def test_una_seccion_rota_no_tumba_el_inventario(self):
        def rota():
            raise RuntimeError(f"fallo leyendo http://u:{SECRETO}@h/x")
        r = inv.recolectar({"ok": lambda: {"a": 1}, "rota": rota})
        self.assertEqual(r["ok"], {"a": 1})
        self.assertIsNone(r["rota"])
        self.assertIn("RuntimeError", r["errores"]["rota"])
        self.assertNotIn(SECRETO, r["errores"]["rota"])

    def test_escritura_atomica_permisos_y_sin_temporales(self):
        salida = self.tmp / "out" / "inventario.json"
        salida.parent.mkdir()
        inv.escribir({"version": 1, "generado_utc": "x", "errores": {}}, salida, grupo=None)
        self.assertEqual(json.loads(salida.read_text())["version"], 1)
        self.assertEqual([p.name for p in salida.parent.iterdir()], ["inventario.json"])
        if os.name == "posix":
            self.assertEqual(oct(salida.stat().st_mode & 0o777), "0o640")

    def test_salida_gigante_se_descarta_con_error_visible(self):
        salida = self.tmp / "inventario.json"
        with mock.patch.object(inv, "MAX_SALIDA", 100):
            inv.escribir({"version": 1, "generado_utc": "x", "errores": {}, "x": "y" * 500}, salida, None)
        d = json.loads(salida.read_text())
        self.assertNotIn("x", d)
        self.assertIn("_salida", d["errores"])

    def test_solo_escribe_el_archivo_de_salida(self):
        self.escribir("opt/destroyer/cap/a.py", "x\n")
        self.escribir("etc/mediadev-db.env", f"P={SECRETO}")
        self.escribir("cron/root", "0 * * * * x\n")
        (self.tmp / "salida").mkdir()
        antes = huella(self.tmp)
        r = inv.recolectar({"codigo_sin_git": lambda: inv.arbol_hashes([self.tmp / "opt/destroyer"]),
                            "secretos_meta": lambda: inv.secretos_meta([(self.tmp / "etc", True)]),
                            "cron": lambda: inv.cron([self.tmp / "cron"])})
        inv.escribir(r, self.tmp / "salida" / "inventario.json", None)
        despues = huella(self.tmp)
        self.assertEqual(set(despues) - set(antes), {"salida/inventario.json"})
        self.assertEqual({k: v for k, v in despues.items() if k in antes}, antes)
        self.assertNotIn(SECRETO, (self.tmp / "salida" / "inventario.json").read_text())


class Instalacion(unittest.TestCase):
    """Los archivos de instalación son la garantía técnica: que no se degraden sin que falle CI."""
    INV = "/var/lib/mediacap-auditor/inventario.json"

    def leer(self, nombre):
        return (DIR_REC / "instalar" / nombre).read_text(encoding="utf-8")

    def test_servicio_es_sandbox_de_solo_lectura(self):
        s = self.leer("mediacap-auditor-inventario.service")
        for linea in ("ProtectSystem=strict", "ReadWritePaths=/var/lib/mediacap-auditor", "NoNewPrivileges=yes",
                      "PrivateNetwork=yes", "ProtectHome=yes", "Nice=19", "IOSchedulingClass=idle"):
            self.assertIn(linea, s.splitlines())
        self.assertEqual(len(re.findall(r"^ReadWritePaths=", s, re.M)), 1)
        self.assertNotRegex(s, r"(?m)^User=")                      # root, pero encerrado por el sandbox

    def test_sshd_fuerza_cat_y_cierra_todo_lo_demas(self):
        s = self.leer("sshd_auditor.conf")
        self.assertIn("Match User auditor", s)
        self.assertIn(f"ForceCommand /bin/cat {self.INV}", s)
        for linea in ("AllowTcpForwarding no", "AllowAgentForwarding no", "AllowStreamLocalForwarding no",
                      "PermitTunnel no", "X11Forwarding no", "PermitTTY no", "PasswordAuthentication no",
                      "AuthorizedKeysFile /etc/ssh/authorized_keys/%u"):
            self.assertIn(linea, s)

    def test_llave_con_restrict_from_y_command(self):
        llave = [l for l in self.leer("authorized_keys.ejemplo").splitlines() if l and not l.startswith("#")]
        self.assertEqual(len(llave), 1)
        self.assertTrue(llave[0].startswith(f'restrict,from="10.136.0.6",command="/bin/cat {self.INV}" '))

    def test_rutas_consistentes(self):
        self.assertEqual(inv.SALIDA.as_posix(), self.INV)
        permisos = self.leer("permisos_mediacap.txt")
        self.assertIn(f"{self.INV}  ", permisos.replace("   ", "  ").replace("   ", "  "))
        self.assertIn("/usr/local/bin/mediacap-auditor-inventario", self.leer("mediacap-auditor-inventario.service"))
        for linea in permisos.splitlines():
            if linea.strip() and not linea.startswith("#"):
                ruta, dueno, grupo, modo = linea.split()
                self.assertEqual(dueno, "root", ruta)               # nada es del auditor
                self.assertNotRegex(modo, r"[2367]$", ruta)          # nadie más escribe


if __name__ == "__main__":
    unittest.main()
