"""Pruebas del uploader v2 + bandeja de salida (scripts/video_segment_uploader.py).

    python -m unittest discover -s tests -v

Sin S3 real, sin DB y (salvo la clase de integración) sin ffmpeg: S3 es un cliente falso que
se puede "caer", "colgar" o "volver lento"; extracción y concat son stubs; el reloj es
controlado. Escala: "1 GB" = 1 MB.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import threading
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
TV = ["canal_5", "canal_6", "hch_tv", "teleceiba", "canal_10", "canal_11"]


class FakeS3:
    """up=False -> todo falla como la suspensión de AWS. fail_keys: claves que fallan una vez.
    gate: threading.Event que bloquea las subidas de audio horario (S3 colgado).
    slow_s: segundos de reloj simulado que consume cada subida (S3 lento)."""

    def __init__(self, clock):
        self.up, self.objects, self.calls, self.puts = False, {}, 0, []
        self.manifests, self.fail_keys, self.gate, self.slow_s, self.clock = {}, set(), None, 0, clock
        self.lock = threading.Lock()

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.calls += 1
        if self.gate is not None and not key.startswith("video_segments/"):
            self.gate.wait(10)
        if not self.up:
            raise Exception("InvalidAccessKeyId: account suspended")
        if key in self.fail_keys:
            self.fail_keys.discard(key)
            raise Exception("connection reset")
        self.clock[0] += self.slow_s
        with self.lock:
            self.objects[key] = os.path.getsize(path)
            self.puts.append(key)
            if key.endswith(".manifest.json"):
                self.manifests[key] = Path(path).read_text()

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
        self.s3 = FakeS3(self.clock)
        self.free = 50 * GB
        self.corrupt = set()          # segmentos cuyo audio no se puede extraer
        self.registered = []          # llamadas a _db_register (s3_scan_log)
        p = mock.patch.multiple(
            vsu, STREAMS_ROOT=self.root, AUDIO_DIR=self.audio, INVALID_DIR=t / "streams" / "_invalid",
            GB=GB, VIDEO_VALIDATE_FFPROBE=False, TV_SHED_MODE="observe",
            _now=lambda: self.clock[0], _ffmpeg_extract=self._extract, _probe_duration=lambda p: 3.96,
            _ffmpeg_concat=self._concat, _coverage_table_exists=lambda: False,
            _db_register=lambda key, *a, **k: self.registered.append(key), _pg_write=lambda *a, **k: None,
            _s3_down_until=0.0, _s3_backoff=vsu.S3_BACKOFF_MIN, _ob_down_until=0.0,
            _ob_backoff=vsu.S3_BACKOFF_MIN, _loop_n=0, _last_marker_gc=0.0)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        vsu._extract_fails.clear()
        vsu._video_coverage.clear()
        vsu._sealing.clear()
        while not vsu._seal_q.empty():
            vsu._seal_q.get_nowait()
        sleep = mock.patch.object(vsu.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    # stubs ---------------------------------------------------------------
    def _extract(self, src, dst):
        if str(src) in self.corrupt:
            return False
        shutil.copyfile(src, dst)
        return True

    @staticmethod
    def _concat(list_txt, out):
        files = [l[6:-1] for l in Path(list_txt).read_text().splitlines() if l.startswith("file '")]
        with open(out, "wb") as o:
            for f in files:
                o.write(Path(f).read_bytes())
        return mock.Mock(returncode=0, stderr="")

    # helpers -------------------------------------------------------------
    def series(self, sid, n, oldest_min, step_s=60, size=GB // 20, start_idx=1000):
        """n segmentos cada step_s, el más viejo de oldest_min; idx crece con el tiempo."""
        d = self.root / sid
        d.mkdir(exist_ok=True)
        out = []
        for i in range(n):
            p = d / f"seg_{start_idx + i:05d}.ts"
            p.write_bytes(b"\1" * size)
            mt = self.clock[0] - oldest_min * 60 + i * step_s
            os.utime(p, (mt, mt))
            out.append(p)
        return out

    def loop(self, sids, advance=15, seal=True):
        """Una vuelta principal; con seal=True además corre el hilo de sellado (sincrónico)."""
        r = vsu.run_once(self.s3, sids, free_fn=lambda _p: self.free)
        if seal:
            vsu.seal_pending()
        self.clock[0] += advance
        return r

    def outbox(self):
        return vsu.process_outbox_once(self.s3)

    def audio_of(self, sid, seg):
        return vsu._audio_seg_path(sid, int(seg.stat().st_mtime) - vsu.SEGMENT_DUR)

    def audio_puts(self):
        return [k for k in self.s3.puts if not k.startswith("video_segments/")]

    def pending_outbox(self):
        return [(e[1], e[3]["label"]) for e in vsu.outbox_entries()]


# ══ El escenario de emergencia de la etapa A (sin cambios de comportamiento) ══
class EmergenciaVariosCanalesS3Caido(Base):
    def setUp(self):
        super().setUp()
        self.sids = ["canal_5", "canal_6", "hch_tv"]
        self.segs = {sid: self.series(sid, 150, oldest_min=150) for sid in self.sids}
        self.corrupt = {str(p) for p in self.segs["canal_6"][:5]}

    def test_s3_caido_no_bloquea_y_todos_los_canales_extraen_audio(self):
        self.loop(self.sids)
        for sid in self.sids:
            sin_audio = [p for p in self.segs[sid][:-12]
                         if p.exists() and not self.audio_of(sid, p).exists()
                         and not vsu._flushed_marker(sid, vsu._hour_of(int(p.stat().st_mtime) - 4)).exists()]
            self.assertEqual(len(sin_audio), 5 if sid == "canal_6" else 0, sid)
        self.assertLessEqual(self.s3.calls, 1)

    def test_breaker_acota_los_intentos_mientras_s3_sigue_caido(self):
        for _ in range(40):
            self.loop(self.sids)
            self.outbox()
        self.assertLessEqual(self.s3.calls, 8)   # video: 60->120->240 s; bandeja: idem

    def test_enforce_descarta_lo_mas_viejo_con_audio_y_conserva_el_resto(self):
        vsu.TV_SHED_MODE = "enforce"
        self.loop(self.sids)
        self.free = 14 * GB
        r = self.loop(self.sids)["shed"]
        self.assertTrue(r["emergency"])
        self.assertEqual(len(r["shed"]), 120)
        self.assertTrue(all(p.exists() for p in self.segs["canal_6"][:5]))     # sin audio: nunca
        self.assertTrue(all(all(p.exists() for p in s[-12:]) for s in self.segs.values()))


# ══ Desacople: la extracción y el sellado no dependen de S3 ══
class Desacople(Base):
    def test_con_s3_caido_la_hora_se_sella_en_disco_y_nada_se_pierde(self):
        segs = self.series("canal_5", 90, oldest_min=90)      # cubre la hora anterior
        self.loop(["canal_5"])
        h = HOUR0 - 3600
        self.assertEqual(vsu._flushed_marker("canal_5", h).read_text(), "sealed")
        self.assertEqual(self.pending_outbox(), [("canal_5", vsu._hour_label(h))])
        e = vsu.outbox_entries()[0][2]
        self.assertTrue((e / "manifest.json").exists() and (e / f"{vsu._hour_label(h)}.ts").exists())
        r = self.outbox()                                      # S3 caído
        self.assertEqual(r["failed"], 1)
        self.assertEqual(json.loads((e / "meta.json").read_text())["attempts"], 1)
        self.assertTrue(e.exists())                            # se conserva para reintentar
        self.assertEqual(self.registered, [])

    def test_al_volver_s3_sube_audio_luego_manifest_y_registra_una_vez(self):
        self.series("canal_5", 90, oldest_min=90)
        self.loop(["canal_5"])
        self.outbox()                                          # falla: backoff
        self.s3.up = True
        self.clock[0] += 3600
        r = self.outbox()
        self.assertEqual(r["uploaded"], 1)
        h = HOUR0 - 3600
        key, mkey = vsu._audio_s3_key("canal_5", h), vsu._manifest_s3_key("canal_5", h)
        self.assertEqual(self.audio_puts(), [key, mkey])         # orden: audio, después manifest
        self.assertEqual(self.registered, [key])
        self.assertEqual(vsu._flushed_marker("canal_5", h).read_text(), "uploaded")
        self.assertEqual(self.pending_outbox(), [])
        self.assertEqual(len(json.loads(self.s3.manifests[mkey])), 60)

    def test_s3_colgado_no_frena_la_vuelta_principal(self):
        """Hilo real de la bandeja bloqueado en S3; la vuelta principal sigue extrayendo."""
        self.s3.up, self.s3.gate = True, threading.Event()
        self.series("canal_5", 90, oldest_min=90)
        self.loop(["canal_5"])                                 # sella la hora anterior
        w = vsu.OutboxWorker(s3_client=self.s3, interval=0.01)
        w.start()
        try:
            for i in range(20):                                # llegan segmentos nuevos
                self.series("canal_5", 4, oldest_min=0.25, step_s=4, start_idx=2000 + i * 4)
                self.loop(["canal_5"])
                d = self.root / "canal_5"
                newest = sorted(d.glob("seg_*.ts"), key=vsu._seg_index)[-1]
                sin_audio = [p for p in d.glob("seg_*.ts") if p != newest
                             and not self.audio_of("canal_5", p).exists()
                             and not vsu._flushed_marker("canal_5", vsu._hour_of(int(p.stat().st_mtime) - 4)).exists()]
                self.assertEqual(sin_audio, [], f"vuelta {i}")
            self.assertEqual(self.audio_puts(), [])              # sigue colgado
        finally:
            self.s3.gate.set()
            w.stop_evt.set()
            w.join(5)
        vsu.process_outbox_once(self.s3, ignore_backoff=True)
        self.assertEqual(self.pending_outbox(), [])
        self.assertEqual(len(self.audio_puts()), len(set(self.audio_puts())))  # sin duplicados

    def test_sellado_lento_en_su_hilo_no_frena_la_extraccion(self):
        """Hilo real de sellado trabado en un concat lento; la vuelta principal sigue."""
        gate, started = threading.Event(), threading.Event()
        real = self._concat

        def slow_concat(lst, out):
            started.set()
            gate.wait(10)
            return real(lst, out)
        self.series("canal_5", 90, oldest_min=90)
        w = vsu.SealWorker(interval=0.01)
        with mock.patch.object(vsu, "_ffmpeg_concat", slow_concat):
            w.start()
            try:
                self.loop(["canal_5"], seal=False)                    # encola la hora anterior
                self.assertTrue(started.wait(5))                      # el hilo está sellando
                h = HOUR0 - 3600
                for i in range(10):
                    self.series("canal_5", 4, oldest_min=0.25, step_s=4, start_idx=3000 + i * 4)
                    late = self.root / "canal_5" / f"seg_{2900 + i:05d}.ts"   # tardío de la hora en sellado
                    late.write_bytes(b"\1" * 2000)
                    os.utime(late, (h + 3500, h + 3500))
                    self.loop(["canal_5"], seal=False)
                    d = self.root / "canal_5"
                    newest = sorted(d.glob("seg_*.ts"), key=vsu._seg_index)[-1]
                    sin_audio = [p for p in d.glob("seg_*.ts") if p != newest and p.stat().st_mtime > h + 3600
                                 and not self.audio_of("canal_5", p).exists()]
                    self.assertEqual(sin_audio, [], f"vuelta {i}")
            finally:
                gate.set()
                w.stop_evt.set()
                w.join(5)
        self.assertEqual(vsu._flushed_marker("canal_5", h).read_text(), "sealed")
        self.assertEqual(len(self.pending_outbox()), 1)
        self.assertFalse((self.audio / "canal_5" / vsu._hour_label(h)).exists())   # no se recreó

    def test_s3_lento_para_video_respeta_el_presupuesto_de_la_vuelta(self):
        self.s3.up, self.s3.slow_s = True, 2.0                   # cada subida "tarda" 2 s
        for sid in TV:
            self.series(sid, 100, oldest_min=10, step_s=4)
        t0 = self.clock[0]
        r = vsu.run_once(self.s3, TV, free_fn=lambda _p: self.free)
        subidos = sum(r["uploaded"].values())
        self.assertLessEqual(self.clock[0] - t0, vsu.UPLOAD_TIME_BUDGET_S + 2.0)
        self.assertEqual(subidos, 11)                            # 20 s / 2 s, +1 que cruza el tope
        self.assertTrue(all(n > 0 for n in r["uploaded"].values()))   # round-robin: todos avanzan


# ══ Recuperación tras cortes en cada punto ══
class Recuperacion(Base):
    def test_corte_a_mitad_del_sellado_deja_tmp_que_no_se_sube_y_se_rehace(self):
        self.series("canal_5", 90, oldest_min=90)
        h = HOUR0 - 3600
        tmp = self.audio / "canal_5" / ".outbox" / f".tmp-{vsu._hour_label(h)}"
        tmp.mkdir(parents=True)
        (tmp / f"{vsu._hour_label(h)}.ts").write_bytes(b"parcial")
        self.s3.up = True
        self.assertEqual(self.outbox()["uploaded"], 0)          # .tmp nunca se sube
        self.loop(["canal_5"])
        self.assertFalse(tmp.exists())
        self.assertEqual(self.outbox()["uploaded"], 1)
        self.assertGreater(self.s3.objects[vsu._audio_s3_key("canal_5", h)], 100)   # la hora real, no el parcial

    def test_corte_entre_rename_y_marcador_no_duplica(self):
        segs = self.series("canal_5", 90, oldest_min=90)
        h = HOUR0 - 3600
        with mock.patch.object(vsu, "_mark_flushed", side_effect=RuntimeError("corte")):
            self.loop(["canal_5"])                           # corte justo después del rename
        self.assertTrue((self.audio / "canal_5" / vsu._hour_label(h)).exists())  # mini-segs aún
        self.assertFalse(vsu._flushed_marker("canal_5", h).exists())
        self.assertEqual(len(self.pending_outbox()), 1)
        self.loop(["canal_5"])                                   # "reinicio"
        self.assertEqual(vsu._flushed_marker("canal_5", h).read_text(), "sealed")
        self.assertFalse((self.audio / "canal_5" / vsu._hour_label(h)).exists())
        self.assertEqual(len(self.pending_outbox()), 1)

    def test_corte_tras_subir_audio_y_antes_del_manifest(self):
        self.series("canal_5", 90, oldest_min=90)
        self.loop(["canal_5"])
        h = HOUR0 - 3600
        key, mkey = vsu._audio_s3_key("canal_5", h), vsu._manifest_s3_key("canal_5", h)
        self.s3.up = True
        self.s3.fail_keys = {mkey}
        self.assertEqual(self.outbox()["failed"], 1)
        self.assertEqual(self.registered, [])                    # no se registra a medias
        self.clock[0] += 3600
        self.assertEqual(self.outbox()["uploaded"], 1)
        self.assertEqual(self.audio_puts().count(key), 1)        # el audio no se resube
        self.assertEqual(self.audio_puts().count(mkey), 1)
        self.assertEqual(self.registered, [key])

    def test_bandeja_sobrevive_al_reinicio_del_proceso(self):
        self.series("canal_5", 90, oldest_min=90)
        self.loop(["canal_5"])
        vsu._ob_down_until = 0.0                                 # "proceso nuevo": estado en memoria vacío
        vsu._extract_fails.clear()
        self.s3.up = True
        self.assertEqual(self.outbox()["uploaded"], 1)

    def test_entrada_sin_meta_no_se_sube(self):
        d = self.audio / "canal_5" / ".outbox" / "2027-01-15T07Z"
        d.mkdir(parents=True)
        (d / "2027-01-15T07Z.ts").write_bytes(b"x" * 100)
        self.s3.up = True
        self.assertEqual(self.outbox(), {"uploaded": 0, "already_in_s3": 0, "corrupt": 0,
                                         "failed": 0, "pending": 0})
        self.assertEqual(self.audio_puts(), [])

    def test_entrada_alterada_se_aparta_y_no_entra_en_bucle(self):
        self.series("canal_5", 90, oldest_min=90)
        self.loop(["canal_5"])
        e = vsu.outbox_entries()[0][2]
        (e / f"{e.name}.ts").write_bytes(b"truncado")
        self.s3.up = True
        self.assertEqual(self.outbox()["corrupt"], 1)
        self.assertTrue((e.parent / f".corrupt-{e.name}").exists())
        self.assertEqual(self.outbox()["corrupt"], 0)
        self.assertEqual(self.audio_puts(), [])

    def test_hora_ya_en_s3_con_archivo_mayor_no_se_reescribe(self):
        self.series("canal_5", 90, oldest_min=90)
        self.loop(["canal_5"])
        h = HOUR0 - 3600
        key, mkey = vsu._audio_s3_key("canal_5", h), vsu._manifest_s3_key("canal_5", h)
        self.s3.objects[key] = 10 ** 9
        self.s3.up = True
        self.assertEqual(self.outbox()["already_in_s3"], 1)
        self.assertEqual(self.audio_puts(), [])                  # ni audio ni manifest
        self.assertEqual(vsu._flushed_marker("canal_5", h).read_text(), "already_in_s3")

    def test_drain_outbox_sube_todo_e_ignora_backoff(self):
        for sid in ["canal_5", "canal_6"]:
            self.series(sid, 90, oldest_min=90)
        self.loop(["canal_5", "canal_6"])
        self.outbox()                                            # falla: backoff
        self.s3.up = True
        with mock.patch.object(vsu, "get_s3", return_value=self.s3):
            self.assertEqual(vsu.drain_outbox(), 0)
        self.assertEqual(len(self.registered), 2)

    def test_canal_detenido_no_retiene_su_ultima_hora(self):
        segs = self.series("tnh", 20, oldest_min=100)
        self.loop(["tnh"])
        self.clock[0] += 3600
        self.loop(["tnh"])
        self.assertTrue(all(p.exists() for p in segs[-12:]))
        self.assertTrue(self.pending_outbox() or
                        any(m.read_text() == "skipped" for m in (self.audio / "tnh" / ".flushed").iterdir()))


# ══ Garantías que se mantienen de la etapa A ══
class Garantias(Base):
    def test_video_sin_audio_no_se_sube_ni_se_borra(self):
        segs = self.series("canal_5", 20, oldest_min=60)
        self.corrupt = {str(segs[0])}
        self.s3.up = True
        self.loop(["canal_5"])
        self.assertTrue(segs[0].exists())
        self.assertFalse(segs[1].exists())

    def test_audio_irrecuperable_tras_3_fallos_libera_el_video(self):
        segs = self.series("canal_5", 20, oldest_min=60)
        self.corrupt = {str(segs[0])}
        self.s3.up = True
        for _ in range(4):
            self.loop(["canal_5"])
        self.assertFalse(segs[0].exists())

    def test_hora_cerrada_no_se_rearma_con_segmentos_tardios(self):
        self.series("canal_5", 90, oldest_min=90)
        self.s3.up = True
        self.loop(["canal_5"])
        self.outbox()
        late = self.root / "canal_5" / "seg_00999.ts"
        late.write_bytes(b"\1" * 2000)
        os.utime(late, (HOUR0 - 3000, HOUR0 - 3000))
        self.loop(["canal_5"])
        self.outbox()
        h = HOUR0 - 3600
        self.assertEqual(self.registered.count(vsu._audio_s3_key("canal_5", h)), 1)
        self.assertFalse((self.audio / "canal_5" / vsu._hour_label(h)).exists())

    def test_sellado_invalido_no_se_reintenta_en_bucle(self):
        self.series("canal_5", 90, oldest_min=90)
        calls = []

        def bad_concat(lst, out):
            calls.append(out)
            return mock.Mock(returncode=1, stderr="boom")
        with mock.patch.object(vsu, "_ffmpeg_concat", bad_concat):
            for _ in range(5):
                self.loop(["canal_5"])
        self.assertEqual(len(calls), 1)
        h = HOUR0 - 3600
        self.assertEqual(vsu._flushed_marker("canal_5", h).read_text(), "invalid")
        self.assertTrue((self.audio / "canal_5" / vsu._hour_label(h)).exists())   # para revisión

    def test_extraccion_guarda_la_duracion(self):
        segs = self.series("canal_5", 20, oldest_min=60)
        self.loop(["canal_5"], seal=False)                       # antes de que se selle la hora
        self.assertEqual(self.audio_of("canal_5", segs[0]).with_suffix(".dur").read_text(), "3.960000")

    def test_modo_off_no_evalua_emergencia(self):
        vsu.TV_SHED_MODE = "off"
        segs = self.series("canal_5", 60, oldest_min=120)
        self.free = 1 * GB
        self.assertFalse(self.loop(["canal_5"])["shed"]["emergency"])
        self.assertTrue(all(p.exists() for p in segs))


# ══ Medición: segmentos pendientes alrededor del cambio de hora ══
class MedicionBacklog(Base):
    """6 canales, 4 segmentos nuevos por canal y vuelta (15 s), 12 min alrededor del cambio de
    hora. Mide, tras cada vuelta, los segmentos sin audio extraído y los de video pendientes
    (fuera de los 12 del HLS). Se corre con S3 sano, caído y colgado (hilo real)."""

    def _simular(self, modo):
        self.clock[0] = float(HOUR0 + 3600 - 360)                # 6 min antes del cambio de hora
        if modo == "sano":
            self.s3.up = True
        elif modo == "colgado":
            self.s3.up, self.s3.gate = True, threading.Event()
        w = None
        if modo == "colgado":
            w = vsu.OutboxWorker(s3_client=self.s3, interval=0.01)
            w.start()
        idx, max_sin_audio, max_video = 0, 0, 0
        try:
            for vuelta in range(48):                             # 48 x 15 s = 12 min
                for sid in TV:
                    self.series(sid, 4, oldest_min=0.25, step_s=4, start_idx=5000 + idx)
                idx += 4
                self.loop(TV)
                if modo == "sano":
                    self.outbox()
                for sid in TV:
                    d = self.root / sid
                    segs = sorted(d.glob("seg_*.ts"), key=vsu._seg_index)
                    sa = sum(1 for p in segs[:-1] if not self.audio_of(sid, p).exists()
                             and not vsu._flushed_marker(sid, vsu._hour_of(int(p.stat().st_mtime) - 4)).exists())
                    max_sin_audio = max(max_sin_audio, sa)
                    max_video = max(max_video, max(0, len(segs) - 12))
        finally:
            if w:
                self.s3.gate.set()
                w.stop_evt.set()
                w.join(5)
        print(f"\n  S3 {modo:<8} max_sin_audio={max_sin_audio}  max_video_pendiente={max_video}  "
              f"horas_selladas={sum(1 for _ in self.audio.glob('*/.flushed/*'))}")
        return max_sin_audio, max_video

    def test_s3_sano(self):
        sa, vid = self._simular("sano")
        self.assertLess(sa, 10)
        self.assertLess(vid, 10)

    def test_s3_caido(self):
        sa, vid = self._simular("caido")
        self.assertLess(sa, 10)                                  # el video se acumula; el audio no

    def test_s3_colgado(self):
        sa, vid = self._simular("colgado")
        self.assertLess(sa, 10)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requiere ffmpeg/ffprobe")
class IntegracionFfmpegReal(Base):
    """Sin stubs de ffmpeg: segmentos TV reales -> extracción (+.dur) -> sellado -> bandeja -> S3 falso."""

    def setUp(self):
        super().setUp()
        p = mock.patch.multiple(vsu, _ffmpeg_extract=_REAL_EXTRACT, _ffmpeg_concat=_REAL_CONCAT,
                                _probe_duration=_REAL_PROBE)
        p.start()
        self.addCleanup(p.stop)

    def test_extraccion_sellado_y_subida_reales(self):
        import subprocess
        d = self.root / "canal_11"
        d.mkdir()
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050", "-t", "64",
                        "-c:v", "libx264", "-g", "40", "-c:a", "aac", "-b:a", "64k", "-f", "hls",
                        "-hls_time", "4", "-hls_list_size", "0", "-hls_segment_filename",
                        str(d / "seg_%05d.ts"), str(d / "index.m3u8")], check=True)
        h = HOUR0 - 3600
        for i, p in enumerate(sorted(d.glob("seg_*.ts"))):
            os.utime(p, (h + 600 + i * 4, h + 600 + i * 4))
        self.clock[0] = float(h + 3600 + 600)
        self.loop(["canal_11"])                                  # extrae + sella, sin S3
        e = vsu.outbox_entries()[0][2]
        man = json.loads((e / "manifest.json").read_text())
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", str(e / f"{e.name}.ts")],
                                   capture_output=True, text=True).stdout)
        self.assertAlmostEqual(sum(x["duration"] for x in man), dur, delta=0.5)
        self.s3.up = True
        self.assertEqual(self.outbox()["uploaded"], 1)
        self.assertGreater(self.s3.objects[vsu._audio_s3_key("canal_11", h)], 10_000)
        self.assertEqual(len(json.loads(self.s3.manifests[vsu._manifest_s3_key("canal_11", h)])), len(man))


_REAL_EXTRACT = vsu._ffmpeg_extract
_REAL_CONCAT = vsu._ffmpeg_concat
_REAL_PROBE = vsu._probe_duration

if __name__ == "__main__":
    unittest.main()
