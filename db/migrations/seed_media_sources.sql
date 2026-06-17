BEGIN;

DO $$
DECLARE
  v_xy_hrn          UUID;
  v_xy_tgu          UUID;
  v_xy_sps          UUID;
  v_radio_satelite  UUID;
  v_fm_941          UUID;
  v_suave_fm        UUID;
  v_radio_america   UUID;
  v_radio_globo     UUID;
  v_radio_el_patio  UUID;
  v_hch_tv          UUID;
  v_teleceiba       UUID;
  v_radio_choluteca UUID;
  v_canal_11        UUID;
  v_canal_6         UUID;
  v_canal_5         UUID;
  v_tsi             UUID;
  v_tenant_id  UUID    := '87c64321-b6bc-4abe-9be2-183c9120a21a';
  v_gw_hn02    INTEGER := 3;
BEGIN

  -- ── media_sources (16 señales) ──────────────────────────────
  INSERT INTO media_sources (stream_catalog_id, name, media_type, country)
    VALUES ('xy_hrn', 'XY HRN', 'radio', 'HN')
    RETURNING id INTO v_xy_hrn;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country)
    VALUES ('xy_tgu', 'XY TGU', 'radio', 'HN')
    RETURNING id INTO v_xy_tgu;

  -- xy_sps: en stations.json pero no en stream_catalog activo
  INSERT INTO media_sources (name, media_type, country)
    VALUES ('XY SPS', 'radio', 'HN')
    RETURNING id INTO v_xy_sps;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country)
    VALUES ('radio_satelite', 'Radio Satelite', 'radio', 'HN')
    RETURNING id INTO v_radio_satelite;

  -- fm_941: en stations.json pero no en stream_catalog activo
  INSERT INTO media_sources (name, media_type, country, frequency_channel)
    VALUES ('94.1 FM', 'radio', 'HN', '94.1 FM')
    RETURNING id INTO v_fm_941;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country)
    VALUES ('suave_fm', 'Suave FM', 'radio', 'HN')
    RETURNING id INTO v_suave_fm;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country)
    VALUES ('radio_america', 'Radio America', 'radio', 'HN')
    RETURNING id INTO v_radio_america;

  -- radio_globo: no en stream_catalog activo
  INSERT INTO media_sources (name, media_type, country)
    VALUES ('Radio Globo', 'radio', 'HN')
    RETURNING id INTO v_radio_globo;

  -- radio_el_patio: no en stream_catalog activo
  INSERT INTO media_sources (name, media_type, country)
    VALUES ('Radio El Patio', 'radio', 'HN')
    RETURNING id INTO v_radio_el_patio;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country)
    VALUES ('hch_tv', 'HCH TV', 'tv', 'HN')
    RETURNING id INTO v_hch_tv;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country)
    VALUES ('teleceiba', 'TeleCeiba', 'tv', 'HN')
    RETURNING id INTO v_teleceiba;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country)
    VALUES ('radio_choluteca', 'Radio Choluteca', 'radio', 'HN')
    RETURNING id INTO v_radio_choluteca;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country, frequency_channel)
    VALUES ('canal_11', 'Canal 11', 'tv', 'HN', 'Canal 11')
    RETURNING id INTO v_canal_11;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country, frequency_channel)
    VALUES ('canal_6', 'Canal 6', 'tv', 'HN', 'Canal 6')
    RETURNING id INTO v_canal_6;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country, frequency_channel)
    VALUES ('canal_5', 'Canal 5', 'tv', 'HN', 'Canal 5')
    RETURNING id INTO v_canal_5;

  INSERT INTO media_sources (stream_catalog_id, name, media_type, country)
    VALUES ('tsi', 'TSI', 'tv', 'HN')
    RETURNING id INTO v_tsi;

  RAISE NOTICE 'media_sources: 16 insertadas';

  -- ── capture_config (1:1 con media_sources) ──────────────────
  -- Radios via SOCKS5 (gateway hn02=3)
  INSERT INTO capture_config (media_source_id, stream_url, route, gateway_id, mp3_s3_prefix)
    VALUES (v_xy_hrn, 'https://ice42.securenetsystems.net/HRN', 'socks5', v_gw_hn02, 'xy_hrn/');

  INSERT INTO capture_config (media_source_id, stream_url, route, gateway_id, mp3_s3_prefix)
    VALUES (v_xy_tgu, 'https://ice42.securenetsystems.net/XY905', 'socks5', v_gw_hn02, 'xy_tgu/');

  INSERT INTO capture_config (media_source_id, stream_url, route, gateway_id)
    VALUES (v_xy_sps, 'https://ice42.securenetsystems.net/XY1073', 'socks5', v_gw_hn02);

  INSERT INTO capture_config (media_source_id, stream_url, route, gateway_id)
    VALUES (v_radio_satelite, 'http://ice42.securenetsystems.net/SATELITE', 'socks5', v_gw_hn02);

  INSERT INTO capture_config (media_source_id, stream_url, route, gateway_id)
    VALUES (v_fm_941, 'https://ice42.securenetsystems.net/94SUFM', 'socks5', v_gw_hn02);

  INSERT INTO capture_config (media_source_id, stream_url, route, gateway_id)
    VALUES (v_suave_fm, 'http://ice42.securenetsystems.net/SUAVE', 'socks5', v_gw_hn02);

  INSERT INTO capture_config (media_source_id, stream_url, route, gateway_id, mp3_s3_prefix)
    VALUES (v_radio_choluteca, 'https://ice42.securenetsystems.net/CHOLUTEC', 'socks5', v_gw_hn02, 'radio_choluteca/');

  -- Radios directas (CDN / IP publica)
  INSERT INTO capture_config (media_source_id, stream_url, route, mp3_s3_prefix)
    VALUES (v_radio_america, 'https://27403.live.streamtheworld.com/AMERICA_SC', 'direct', 'radio_america/');

  INSERT INTO capture_config (media_source_id, stream_url, route)
    VALUES (v_radio_globo, 'https://stream.radiosmundiales.com/stream/radioglobo', 'direct');

  INSERT INTO capture_config (media_source_id, stream_url, route)
    VALUES (v_radio_el_patio, 'http://51.89.173.53:8045/elpatio', 'direct');

  -- TV directa
  INSERT INTO capture_config (media_source_id, stream_url, route)
    VALUES (v_hch_tv, 'https://live.streamhch.com/live/streams/hch1.m3u8', 'direct');

  INSERT INTO capture_config (media_source_id, stream_url, route)
    VALUES (v_teleceiba, 'http://tvprem.pro:8080/live/Gchiham1/IPTV2207/2723.m3u8', 'direct');

  INSERT INTO capture_config (media_source_id, stream_url, route)
    VALUES (v_canal_11, 'https://redirector.rudo.video/hls-video/c54ac2799874375c81c1672abb700870537c5223/canal11hn/canal11hn.smil/playlist.m3u8', 'direct');

  INSERT INTO capture_config (media_source_id, stream_url, route, ts_s3_prefix)
    VALUES (v_canal_6, 'http://tvprem.pro:8080/live/Gchiham1/IPTV2207/472634.m3u8', 'direct', 'canal_6');

  INSERT INTO capture_config (media_source_id, stream_url, route, ts_s3_prefix)
    VALUES (v_canal_5, 'http://tvprem.pro:8080/live/Gchiham1/IPTV2207/2700.m3u8', 'direct', 'canal_5');

  INSERT INTO capture_config (media_source_id, stream_url, route, ts_s3_prefix)
    VALUES (v_tsi, 'http://tvprem.pro:8080/live/Gchiham1/IPTV2207/2718.m3u8', 'direct', 'tsi');

  RAISE NOTICE 'capture_config: 16 insertadas';

  -- ── tenant_media_source_assignments (todos → MediaAI DEMO) ──
  INSERT INTO tenant_media_source_assignments (tenant_id, media_source_id) VALUES
    (v_tenant_id, v_xy_hrn),
    (v_tenant_id, v_xy_tgu),
    (v_tenant_id, v_xy_sps),
    (v_tenant_id, v_radio_satelite),
    (v_tenant_id, v_fm_941),
    (v_tenant_id, v_suave_fm),
    (v_tenant_id, v_radio_america),
    (v_tenant_id, v_radio_globo),
    (v_tenant_id, v_radio_el_patio),
    (v_tenant_id, v_hch_tv),
    (v_tenant_id, v_teleceiba),
    (v_tenant_id, v_radio_choluteca),
    (v_tenant_id, v_canal_11),
    (v_tenant_id, v_canal_6),
    (v_tenant_id, v_canal_5),
    (v_tenant_id, v_tsi);

  RAISE NOTICE 'tenant_media_source_assignments: 16 asignadas';

END $$;

COMMIT;

-- Verificacion final
SELECT ms.name, ms.media_type, cc.route, cc.gateway_id,
       CASE WHEN tmsa.id IS NOT NULL THEN 'asignada' ELSE 'sin asignar' END AS tenant_status
FROM media_sources ms
JOIN capture_config cc ON cc.media_source_id = ms.id
LEFT JOIN tenant_media_source_assignments tmsa ON tmsa.media_source_id = ms.id
ORDER BY ms.media_type DESC, ms.name;
