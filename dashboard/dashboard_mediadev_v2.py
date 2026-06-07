#!/usr/bin/env python3
import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, jsonify

# Configurar la ruta correcta para templates
app = Flask(__name__, template_folder='/opt/media-ai/dashboard/templates')

STATUS_FILE = '/var/www/streams/status.json'
HLS_BASE = 'http://159.223.104.91:8080'

def load_status():
    if not Path(STATUS_FILE).exists():
        return {'streams': {}, 'summary': {'ok': 0, 'fail': 0, 'total': 0}}
    try:
        with open(STATUS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'Error leyendo {STATUS_FILE}: {e}', file=sys.stderr)
        return {'streams': {}, 'summary': {'ok': 0, 'fail': 0, 'total': 0}}

def enrich_streams(data):
    streams = []
    for stream_id, info in data.get('streams', {}).items():
        status = info.get('status', 'UNKNOWN')
        sup = info.get('sup', 'UNKNOWN')
        segs = info.get('segs', 0)
        age = info.get('age', 0)

        if status == 'OK':
            icon = '✓'
            color_class = 'ok'
            status_label = 'ACTIVO'
        elif status == 'STALE':
            icon = '⚠'
            color_class = 'stale'
            status_label = 'DESACTUALIZADO'
        else:
            icon = '✕'
            color_class = 'error'
            status_label = 'ERROR'

        url = f'{HLS_BASE}/{stream_id}/index.m3u8'

        streams.append({
            'id': stream_id,
            'status': status_label,
            'status_raw': status,
            'color': color_class,
            'icon': icon,
            'sup': sup,
            'segs': segs,
            'age': age,
            'url': url,
        })

    return sorted(streams, key=lambda x: (x['status_raw'] != 'OK', x['id']))

@app.route('/')
def index():
    data = load_status()
    streams = enrich_streams(data)
    summary = data.get('summary', {})
    updated = data.get('updated', '—')
    return render_template('dashboard_mediadev.html',
                          streams=streams,
                          summary=summary,
                          updated=updated,
                          base_url=HLS_BASE)

@app.route('/api/status')
def api_status():
    data = load_status()
    streams = enrich_streams(data)
    return jsonify({
        'streams': streams,
        'summary': data.get('summary', {}),
        'updated': data.get('updated', '—'),
        'ok_count': sum(1 for s in streams if s['status_raw'] == 'OK'),
        'fail_count': sum(1 for s in streams if s['status_raw'] != 'OK'),
    })

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=int(os.getenv('DASHBOARD_PORT', '9000')))
    p.add_argument('--debug', action='store_true')
    args = p.parse_args()

    print(f'\n📺 MediaDEV Stream Dashboard')
    print(f'   http://localhost:{args.port}')
    print(f'   Status: {STATUS_FILE}')
    print(f'\n')

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
