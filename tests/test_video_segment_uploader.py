"""Pruebas del bucle v2 de scripts/video_segment_uploader.py — sin S3, sin ffmpeg, sin DB.

    python -m unittest discover -s tests -v

S3 es un cliente falso que se puede "caer"; ffmpeg (extracción y armado de la hora) está
reemplazado por stubs; el reloj es controlado. Escala: "1 GB" = 1 MB.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("UPLOADER_LOG", "/nonexistent-dir/uploader.log")   # sin FileHandler
_SPEC = importlib.util.spec_from_file_location(
    "video_segment_uploader",
    Path(__file__).resolve().parents[1] / "scripts" / "video_segment_uploader.py")
vsu = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vsu)

GB = 1_000_000
HOUR0 = 1_800_000_000                 # múltiplo de 3600: inicio de hora
NOW0 = HOUR0 + 1800                   # media hora


class FakeS3:
    def __init__(self):
        self.up = False
        self.objects = {}
        self.calls = 0

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.calls += 1
        if not self.up:
            raise Exception("InvalidAccessKeyId: account suspended")
        self.objects[key] = os.path.getsize(path)

    def head_object(self, Bucket, Key):
        self.calls += 1
        if not self.up:
            raise Exception("InvalidAccessKeyId: account suspended")
        if Key not in self.objects:
            e = Exception("Not Found")
            e.response = {"Error": {"Code": "404"}}
            raise e
        return {"ContentLength": self.objects[Key]}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = Path(self._tmp.name)
        self.root, self.audio = t / "streams", t / "streams" / "_tv_audio"
        self.root.mkdir()
        self.clock = [float(NOW0)]
        self.s3 = FakeS3()
        self.free = 50 * GB
        self.corrupt = set()          # segmentos cuyo audio no se puede extraer
        self.flush_calls = []
        p = mock.patch.multiple(
            vsu, STREAMS_ROOT=self.root, AUDIO_DIR=self.audio, INVALID_DIR=t / "streams" / "_invalid",
            GB=GB, VIDEO_VALIDATE_FFPROBE=False, TV_SHED_MODE="observe",
            _now=lambda: self.clock[0], _ffmpeg_extract=self._extract,
            flush_audio_hour=self._flush, _coverage_table_exists=lambda: False,
            _db_register=lambda *a, **k: None, _pg_write=lambda *a, **k: None,
            _s3_down_until=0.0, _s3_backoff=vsu.S3_BACKOFF_MIN, _loop_n=0, _last_marker_gc=0.0)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        vsu._extract_fails.clear()
        vsu._video_coverage.clear()
        sleep = mock.patch.object(vsu.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    # stubs
    def _extract(self, src, dst):
        if str(src) in self.corrupt:
            return False
        shutil.copyfile(src, dst)
        return True

    def _flush(self, s3_client, sid, hour_epoch, segs_dir):
        self.flush_calls.append((sid, hour_epoch))
        if not self.s3.up:
            return "upload_failed"
        s3_client.objects[vsu._audio_s3_key(sid, hour_epoch)] = 1
        shutil.rmtree(segs_dir, ignore_errors=True)
        return "uploaded"

    # helpers
    def series(self, sid, n, oldest_min, step_s=60, size=GB // 20):
        """n segmentos cada step_s, el más viejo de oldest_min; idx crece con el tiempo."""
        d = self.root / sid
        d.mkdir(exist_ok=True)
        out = []
        for i in range(n):
            p = d / f"seg_{1000 + i:05d}.ts"
            p.write_bytes(b"\1" * size)
            mt = self.clock[0] - oldest_min * 60 + i * step_s
            os.utime(p, (mt, mt))
            out.append(p)
        return out

    def loop(self, sids, advance=15):
        r = vsu.run_once(self.s3, sids, free_fn=lambda _p: self.free)
        self.clock[0] += advance
        return r

    def audio_of(self, sid, seg):
        return vsu._audio_seg_path(sid, int(seg.stat().st_mtime) - vsu.SEGMENT_DUR)


class EmergenciaVariosCanalesS3Caido(Base):
    """El escenario pedido: 3 canales con 2.5 h de pendientes, S3 caído, disco en emergencia."""

    def setUp(self):
        super().setUp()
        self.sids = ["canal_5", "canal_6", "hch_tv"]
        self.segs = {sid: self.series(sid, 150, oldest_min=150) for sid in self.sids}
        self.corrupt = {str(p) for p in self.segs["canal_6"][:5]}  # los 5 más viejos de canal_6

    def test_1_s3_caido_no_bloquea_y_todos_los_canales_extraen_audio(self):
        self.loop(self.sids)
        for sid in self.sids:
            elegibles = self.segs[sid][:-12]
            sin_audio = [p for p in elegibles if not self.audio_of(sid, p).exists()]
            esperado = 5 if sid == "canal_6" else 0
            self.assertEqual(len(sin_audio), esperado, sid)
        self.assertLessEqual(self.s3.calls, 1)          # 1 intento y el breaker corta
        self.assertTrue(all(p.exists() for s in self.segs.values() for p in s))

    def test_2_breaker_acota_los_intentos_mientras_s3_sigue_caido(self):
        for _ in range(40):                               # 10 minutos de vueltas
            self.loop(self.sids)
        self.assertLessEqual(self.s3.calls, 4)            # backoff 60 -> 120 -> 240 s

    def test_3_observe_reporta_sin_borrar(self):
        self.loop(self.sids)
        self.free = 14 * GB
        r = self.loop(self.sids)["shed"]
        self.assertTrue(r["emergency"])
        self.assertEqual(len(r["shed"]), 120)             # 6 GB / 0.05 GB
        self.assertTrue(all(p.exists() for s in self.segs.values() for p in s))

    def test_4_enforce_conserva_lo_que_debe_y_descarta_lo_mas_viejo_con_audio(self):
        vsu.TV_SHED_MODE = "enforce"
        self.loop(self.sids)                              # extrae audio
        self.free = 14 * GB
        r = self.loop(self.sids)["shed"]
        now = self.clock[0] - 15
        tabla, borrados_mt, vivos_elegibles_mt = {}, [], []
        for sid in self.sids:
            fila = {"descartado": 0, "conservado_12_mas_nuevos": 0, "conservado_menor_30min": 0,
                    "conservado_sin_audio": 0, "conservado_por_objetivo": 0}
            for i, p in enumerate(self.segs[sid]):
                mt = HOUR0 + 1800 - 150 * 60 + i * 60
                if not p.exists():
                    fila["descartado"] += 1
                    borrados_mt.append(mt)
                    self.assertTrue(vsu._audio_seg_path(sid, mt - vsu.SEGMENT_DUR).exists())
                elif i >= 150 - 12:
                    fila["conservado_12_mas_nuevos"] += 1
                elif now - mt < 30 * 60:
                    fila["conservado_menor_30min"] += 1
                elif str(p) in self.corrupt:
                    fila["conservado_sin_audio"] += 1
                else:
                    fila["conservado_por_objetivo"] += 1
                    vivos_elegibles_mt.append(mt)
            tabla[sid] = fila
        print("\n  canal      descart  12nuevos  <30min  sin_audio  objetivo_ok")
        for sid, f in tabla.items():
            print(f"  {sid:<10} {f['descartado']:>6}  {f['conservado_12_mas_nuevos']:>8}  "
                  f"{f['conservado_menor_30min']:>6}  {f['conservado_sin_audio']:>9}  "
                  f"{f['conservado_por_objetivo']:>11}")
        self.assertEqual(sum(f["descartado"] for f in tabla.values()), 120)
        self.assertEqual(tabla["canal_6"]["conservado_sin_audio"], 5)
        self.assertTrue(all(f["conservado_12_mas_nuevos"] == 12 for f in tabla.values()))
        self.assertLessEqual(max(borrados_mt), min(vivos_elegibles_mt))   # los más viejos primero
        self.assertEqual({p.parent.name for p in map(Path, r["shed"])}, set(self.sids))

    def test_5_al_volver_s3_sube_round_robin_y_cierra_horas_sin_reescribir(self):
        self.loop(self.sids)
        self.s3.up = True
        self.clock[0] += 3600                             # pasa el backoff
        r = self.loop(self.sids)
        self.assertTrue(all(n > 0 for n in r["uploaded"].values()), r["uploaded"])
        self.assertTrue(all(n <= vsu.UPLOAD_MAX_PER_CHANNEL for n in r["uploaded"].values()))
        self.assertGreater(len(self.flush_calls), 0)
        sid, h = self.flush_calls[0]
        self.assertTrue(vsu._flushed_marker(sid, h).exists())


class Garantias(Base):
    def test_video_sin_audio_no_se_sube_ni_se_borra(self):
        segs = self.series("canal_5", 20, oldest_min=60)
        self.corrupt = {str(segs[0])}
        self.s3.up = True
        self.loop(["canal_5"])
        self.assertTrue(segs[0].exists())
        self.assertFalse(segs[1].exists())               # el resto sí se subió

    def test_audio_irrecuperable_tras_3_fallos_libera_el_video(self):
        segs = self.series("canal_5", 20, oldest_min=60)
        self.corrupt = {str(segs[0])}
        self.s3.up = True
        for _ in range(4):
            self.loop(["canal_5"])
        self.assertFalse(segs[0].exists())

    def test_hora_ya_en_s3_con_archivo_mayor_no_se_reescribe(self):
        segs = self.series("canal_5", 80, oldest_min=100)  # cubre la hora anterior
        self.loop(["canal_5"])                              # extrae, S3 caído
        h = HOUR0 - 3600
        self.s3.objects[vsu._audio_s3_key("canal_5", h)] = 10 ** 9
        self.s3.up = True
        self.clock[0] += 3600
        for _ in range(3):                                  # 1 hora por vuelta (FLUSH_MAX_PER_LOOP)
            self.loop(["canal_5"])
        self.assertNotIn(("canal_5", h), self.flush_calls)
        self.assertEqual(vsu._flushed_marker("canal_5", h).read_text(), "already_in_s3")

    def test_hora_cerrada_no_se_vuelve_a_armar_con_segmentos_tardios(self):
        segs = self.series("canal_5", 80, oldest_min=100)
        self.s3.up = True
        self.loop(["canal_5"])
        self.clock[0] += 3600
        self.loop(["canal_5"])
        n = len(self.flush_calls)
        late = self.root / "canal_5" / "seg_00999.ts"     # aparece un segmento de esa hora
        late.write_bytes(b"\1" * 2000)
        os.utime(late, (HOUR0 - 3000, HOUR0 - 3000))
        self.loop(["canal_5"])
        self.assertEqual(len([c for c in self.flush_calls if c[1] == HOUR0 - 3600]), 1)
        self.assertFalse((self.audio / "canal_5" / vsu._hour_label(HOUR0 - 3600)).exists())
        self.assertGreaterEqual(len(self.flush_calls), n)

    def test_flush_invalido_no_se_reintenta_en_bucle(self):
        self.series("canal_5", 80, oldest_min=100)
        self.loop(["canal_5"])
        self.s3.up = True
        self.clock[0] += 3600
        with mock.patch.object(vsu, "flush_audio_hour", lambda *a: self.flush_calls.append(a[1:3]) or "invalid"):
            for _ in range(5):
                self.loop(["canal_5"])
        self.assertEqual(len([c for c in self.flush_calls if c[1] == HOUR0 - 3600]), 1)

    def test_segmento_que_desaparece_no_rompe_la_vuelta(self):
        segs = self.series("canal_5", 20, oldest_min=60)
        self.s3.up = True
        real = vsu.upload_file_verified

        def borra_y_sube(s3, path, *a, **k):
            if path == segs[0]:
                path.unlink()
                raise FileNotFoundError(path)
            return real(s3, path, *a, **k)
        with mock.patch.object(vsu, "upload_file_verified", borra_y_sube):
            r = self.loop(["canal_5"])
        self.assertEqual(r["uploaded"]["canal_5"], 7)    # 20 - 12 protegidos - 1 que desapareció

    def test_canal_detenido_no_retiene_su_ultima_hora(self):
        # el canal dejó de escribir hace 80 min: sus 12 protegidos no se suben, pero su audio
        # se extrae y la hora se cierra (antes quedaba retenida hasta que volviera la señal)
        segs = self.series("tnh", 20, oldest_min=100)
        self.s3.up = True
        self.loop(["tnh"])
        self.assertTrue(all(p.exists() for p in segs[-12:]))
        self.clock[0] += 3600
        self.loop(["tnh"])
        self.assertTrue(any(sid == "tnh" for sid, _ in self.flush_calls))
        self.assertTrue(all(vsu._flushed_marker("tnh", h).exists() for _, h in self.flush_calls))

    def test_modo_off_no_evalua_emergencia(self):
        vsu.TV_SHED_MODE = "off"
        segs = self.series("canal_5", 60, oldest_min=120)
        self.free = 1 * GB
        r = self.loop(["canal_5"])["shed"]
        self.assertFalse(r["emergency"])
        self.assertTrue(all(p.exists() for p in segs))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requiere ffmpeg/ffprobe")
class IntegracionFfmpegReal(Base):
    """Sin stubs de ffmpeg: segmentos TV reales (video+AAC) -> extracción -> flush_audio_hour real."""

    def setUp(self):
        super().setUp()
        self._p = mock.patch.multiple(vsu, _ffmpeg_extract=_REAL_EXTRACT, flush_audio_hour=_REAL_FLUSH)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_extraccion_y_hora_reales(self):
        import subprocess
        d = self.root / "canal_11"
        d.mkdir()
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050", "-t", "64",
                        "-c:v", "libx264", "-g", "40", "-c:a", "aac", "-b:a", "64k", "-f", "hls",
                        "-hls_time", "4", "-hls_list_size", "0", "-hls_segment_filename",
                        str(d / "seg_%05d.ts"), str(d / "index.m3u8")], check=True)
        segs = sorted(d.glob("seg_*.ts"))
        h = HOUR0 - 3600
        for i, p in enumerate(segs):                       # toda la hora anterior, 1 cada 4 s
            os.utime(p, (h + 600 + i * 4, h + 600 + i * 4))
        self.s3.up = True
        self.loop(["canal_11"])
        key = vsu._audio_s3_key("canal_11", h)
        self.assertIn(key, self.s3.objects)
        self.assertGreater(self.s3.objects[key], 10_000)
        self.assertIn(vsu._manifest_s3_key("canal_11", h), self.s3.objects)
        self.assertEqual(vsu._flushed_marker("canal_11", h).read_text(), "uploaded")
        self.assertEqual(len(list(d.glob("seg_*.ts"))), 12)   # subidos todos menos los 12 del HLS


_REAL_EXTRACT = vsu._ffmpeg_extract
_REAL_FLUSH = vsu.flush_audio_hour

if __name__ == "__main__":
    unittest.main()
