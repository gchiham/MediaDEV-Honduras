"""Pruebas de scripts/segment_janitor.py — stdlib unittest, sin red ni DB ni /proc.

    python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "segment_janitor", Path(__file__).resolve().parents[1] / "scripts" / "segment_janitor.py")
sj = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sj)

# Escala de prueba: "1 GB" = 1 MB, para no escribir gigas en disco. El módulo lee GB en runtime.
sj.GB = 1_000_000
GB = sj.GB
NOW = 1_800_000_000.0
TYPES = {"xy_hrn": "radio", "fm_941": "radio", "canal_5": "tv", "canal_6": "tv", "tsi": "tv"}


class Env:
    """STREAMS_ROOT temporal + dependencias falsas (DB, disco, /proc, unlink, Telegram)."""

    def __init__(self, tmp):
        self.root = Path(tmp) / "streams"
        self.root.mkdir()
        self.cfg = sj.Config(streams_root=self.root, enforce=True,
                             cache_path=Path(tmp) / "cache.json",
                             state_path=Path(tmp) / "state.json",
                             lock_path=Path(tmp) / "lock")
        self.db = (dict(TYPES), "PostgreSQL")
        self.free = 50 * GB
        self.open = set()
        self.sent = []

    def seg(self, slug, idx, age_min, size=1000):
        d = self.root / slug
        d.mkdir(exist_ok=True)
        p = d / f"seg_{idx:05d}.ts"
        p.write_bytes(b"\0" * size)
        os.utime(p, (NOW - age_min * 60, NOW - age_min * 60))
        return p

    def series(self, slug, n, oldest_min, step_min, size=1000):
        """n segmentos de oldest_min hacia el presente; idx crece con el tiempo."""
        return [self.seg(slug, i, oldest_min - i * step_min, size) for i in range(n)]

    def run(self, **kw):
        kw.setdefault("now", NOW)
        return sj.run(self.cfg, load_db=lambda: self.db, free_fn=lambda _r: self.free,
                      open_fn=lambda _r: None if self.open is None else set(self.open),
                      alert_sink=self.sent.append, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.e = Env(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class RadioPolicy(Base):
    def test_borra_mayores_a_8h_y_conserva_el_resto(self):
        segs = self.e.series("xy_hrn", 40, oldest_min=12 * 60, step_min=20)  # 12 h -> ~0
        r = self.e.run()
        alive = [p for p in segs if p.exists()]
        self.assertTrue(all((NOW - p.stat().st_mtime) <= 8 * 3600 for p in alive))
        self.assertEqual(r["deleted"]["radio"], 40 - len(alive))
        self.assertGreater(r["deleted"]["radio"], 0)

    def test_protege_los_12_mas_nuevos_aunque_sean_viejos(self):
        segs = self.e.series("xy_hrn", 15, oldest_min=20 * 60, step_min=1)  # todos > 8 h
        self.e.run()
        self.assertEqual(sum(p.exists() for p in segs), 12)
        self.assertTrue(all(p.exists() for p in segs[-12:]))

    def test_protege_lo_referenciado_por_el_playlist(self):
        segs = self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)
        (self.e.root / "xy_hrn" / "index.m3u8").write_text(
            f"#EXTM3U\n#EXTINF:4,\n{segs[0].name}\n")
        self.e.run()
        self.assertTrue(segs[0].exists())

    def test_radio_nunca_se_borra_por_espacio(self):
        segs = self.e.series("xy_hrn", 30, oldest_min=7 * 60, step_min=10)  # todos < 8 h
        self.e.free = 1 * GB
        self.e.run()
        self.assertTrue(all(p.exists() for p in segs))

    def test_modo_observe_no_borra(self):
        self.e.cfg.enforce = False
        segs = self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)
        r = self.e.run()
        self.assertTrue(all(p.exists() for p in segs))
        self.assertEqual(r["mode"], "observe")
        self.assertEqual(r["deleted"]["radio"], 18)        # lo que borraría

    def test_ventana_que_solapa_al_daemon_falla_seguro(self):
        self.e.cfg.radio_keep_hours = 4                     # < AUTO_BACKFILL_HOURS(3) + 2
        segs = self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)
        r = self.e.run()
        self.assertEqual(r["exit"], 1)
        self.assertTrue(all(p.exists() for p in segs))
        self.assertTrue(any("se solapa" in m for m in self.e.sent))


class TvNuncaLaBorraElLimpiador(Base):
    def test_tv_intacta_con_espacio_suficiente(self):
        segs = self.e.series("canal_5", 50, oldest_min=23 * 60, step_min=20)
        r = self.e.run()
        self.assertTrue(all(p.exists() for p in segs))
        self.assertFalse(r["emergency"])

    def test_tv_intacta_incluso_en_emergencia_y_alerta(self):
        segs = self.e.series("canal_5", 60, oldest_min=600, step_min=10, size=GB // 10)
        segs += self.e.series("tsi", 20, oldest_min=600, step_min=10, size=GB)
        self.e.free = 1 * GB
        r = self.e.run()
        self.assertTrue(all(p.exists() for p in segs))
        self.assertTrue(r["emergency"])
        self.assertEqual(r["deleted"]["tv"], 0)
        self.assertTrue(any("TV_SHED_MODE" in m for m in self.e.sent))

    def test_backlog_tv_alerta_y_margen(self):
        self.e.series("canal_5", 40, oldest_min=120, step_min=2, size=1_000_000)
        r = self.e.run()
        self.assertTrue(any("no drena" in m for m in self.e.sent))
        self.assertGreater(r["margin"]["tv_backlog_gb"], 0)


class InUse(Base):
    def test_salta_archivos_abiertos(self):
        segs = self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)
        self.e.open = {str(segs[0])}
        r = self.e.run()
        self.assertTrue(segs[0].exists())
        self.assertEqual(r["skipped_open"], 1)

    def test_sin_proc_no_borra_nada_y_alerta(self):
        segs = self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)
        self.e.open = None
        r = self.e.run()
        self.assertTrue(all(p.exists() for p in segs))
        self.assertEqual(r["exit"], 1)
        self.assertTrue(any("/proc" in m for m in self.e.sent))

    def test_archivo_borrado_por_otro_en_paralelo_no_falla(self):
        self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)

        def racy_unlink(path):
            raise FileNotFoundError(path)
        r = self.e.run(unlink=racy_unlink)
        self.assertEqual(r["exit"], 0)
        self.assertEqual(r["deleted"]["radio"], 0)


class Classification(Base):
    def test_db_ok_escribe_cache(self):
        self.e.run()
        data = json.loads(self.e.cfg.cache_path.read_text())
        self.assertEqual(data["types"], TYPES)

    def test_db_caida_usa_cache_valida(self):
        sj.write_cache(self.e.cfg.cache_path, TYPES, NOW - 3600)
        self.e.db = (None, "conexión PG falló")
        segs = self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)
        r = self.e.run()
        self.assertIn("caché local", r["classification"])
        self.assertEqual(sum(p.exists() for p in segs), 12)

    def test_db_caida_y_cache_vencida_falla_seguro(self):
        sj.write_cache(self.e.cfg.cache_path, TYPES, NOW - 100 * 3600)
        self.e.db = (None, "conexión PG falló")
        segs = self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)
        r = self.e.run()
        self.assertEqual(r["exit"], 1)
        self.assertTrue(all(p.exists() for p in segs))
        self.assertTrue(any("sin clasificación" in m for m in self.e.sent))

    def test_db_caida_sin_cache_falla_seguro(self):
        self.e.db = (None, "conexión PG falló")
        segs = self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)
        r = self.e.run()
        self.assertEqual(r["exit"], 1)
        self.assertTrue(all(p.exists() for p in segs))

    def test_cache_con_tipo_invalido_se_rechaza(self):
        self.e.cfg.cache_path.write_text(json.dumps({"generated_at": NOW, "types": {"x": "podcast"}}))
        types, why = sj.read_cache(self.e.cfg.cache_path, 72, NOW)
        self.assertIsNone(types)
        self.assertIn("inválidos", why)

    def test_slug_desconocido_y_dirs_especiales_no_se_tocan(self):
        unk = self.e.series("suave_fm", 30, oldest_min=20 * 60, step_min=1)
        inv = self.e.series("_invalid", 30, oldest_min=20 * 60, step_min=1)
        r = self.e.run()
        self.assertTrue(all(p.exists() for p in unk + inv))
        self.assertIn("suave_fm", r["unknown_dirs"])

    @unittest.skipIf(sys.platform == "win32", "symlinks requieren privilegios en Windows")
    def test_no_sigue_symlinks(self):
        outside = Path(self._tmp.name) / "fuera"
        outside.mkdir()
        target = outside / "seg_00001.ts"
        target.write_bytes(b"x")
        os.utime(target, (NOW - 86400, NOW - 86400))
        (self.e.root / "xy_hrn").mkdir()
        os.symlink(target, self.e.root / "xy_hrn" / "seg_00001.ts")
        os.symlink(outside, self.e.root / "fm_941")
        self.e.run()
        self.assertTrue(target.exists())

    def test_build_types_descarta_conflictos(self):
        t = sj.build_types([("a", "radio"), ("a", "tv"), ("b", "tv"), ("c", "podcast"), (None, "tv")])
        self.assertEqual(t, {"b": "tv"})


class DryRunAndAlerts(Base):
    def test_dry_run_no_borra_ni_escribe_ni_envia(self):
        segs = self.e.series("xy_hrn", 30, oldest_min=20 * 60, step_min=1)
        r = self.e.run(dry_run=True, simulate_free_gb=5)
        self.assertTrue(all(p.exists() for p in segs))
        self.assertEqual(r["deleted"]["radio"], 18)        # lo que HABRÍA borrado
        self.assertFalse(self.e.cfg.cache_path.exists())
        self.assertFalse(self.e.cfg.state_path.exists())
        self.assertEqual(self.e.sent, [])

    def test_alerta_no_se_repite_dentro_de_la_ventana_y_avisa_al_resolverse(self):
        self.e.series("canal_5", 40, oldest_min=120, step_min=2)  # backlog TV de 2 h
        self.e.run()
        n1 = len(self.e.sent)
        self.assertTrue(any("no drena" in m for m in self.e.sent))
        self.e.run(now=NOW + 600)                                   # 10 min después: sin repetir
        self.assertEqual(len(self.e.sent), n1)
        for p in (self.e.root / "canal_5").iterdir():               # el uploader drenó
            p.unlink()
        self.e.run(now=NOW + 1200)
        self.assertTrue(any("resuelto TV_BACKLOG" in m for m in self.e.sent))

    def test_margen_reportado(self):
        self.e.series("canal_5", 20, oldest_min=2, step_min=0.06, size=1_000_000)
        r = self.e.run()
        m = r["margin"]
        self.assertAlmostEqual(m["tv_ingest_gb_per_h"], 1_000_000 * 900 / GB, places=2)
        self.assertEqual(m["headroom_gb"], 35.0)

    def test_cli_simulate_exige_dry_run(self):
        with self.assertRaises(SystemExit):
            sj.main(["--simulate-free-gb", "5"])


if __name__ == "__main__":
    unittest.main()
