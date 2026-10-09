#!/usr/bin/env python3
"""
MediaDEV CloudWatch Metrics Pusher
Pushes system + process metrics to CloudWatch namespace MediaDEV every 30s.
Runs as a systemd service on mediaCAP and mediaAPP.
"""
import os, time, socket, subprocess, sys
import psutil
import boto3

SERVER    = os.environ.get("CW_SERVER_NAME", socket.gethostname())
NAMESPACE = "MediaDEV"
INTERVAL  = 30
REGION    = "us-east-1"

cw = boto3.client(
    "cloudwatch",
    region_name=REGION,
    aws_access_key_id=os.environ["CW_AWS_KEY"],
    aws_secret_access_key=os.environ["CW_AWS_SECRET"],
)

DIM = [{"Name": "Server", "Value": SERVER}]

def m(name, value, unit="None"):
    return {"MetricName": name, "Value": float(value), "Unit": unit, "Dimensions": DIM}

def proc_count(pattern):
    try:
        out = subprocess.check_output(["pgrep", "-c", "-f", pattern], text=True).strip()
        return int(out)
    except Exception:
        return 0

def collect():
    data = []

    # CPU
    cpu = psutil.cpu_percent(interval=1)
    data.append(m("cpu_used_pct", cpu, "Percent"))

    # RAM
    mem = psutil.virtual_memory()
    data.append(m("mem_used_pct", mem.percent, "Percent"))
    data.append(m("mem_available_mb", mem.available / 1024**2, "Megabytes"))

    # Disco /
    disk = psutil.disk_usage("/")
    data.append(m("disk_used_pct", disk.percent, "Percent"))
    data.append(m("disk_free_gb", disk.free / 1024**3, "Gigabytes"))

    # Disco /opt (si existe)
    try:
        opt = psutil.disk_usage("/opt")
        data.append(m("disk_opt_used_pct", opt.percent, "Percent"))
    except Exception:
        pass

    # Red eth0
    try:
        net_before = psutil.net_io_counters(pernic=True).get("eth0")
        time.sleep(1)
        net_after = psutil.net_io_counters(pernic=True).get("eth0")
        if net_before and net_after:
            data.append(m("net_bytes_sent_per_s",
                          (net_after.bytes_sent - net_before.bytes_sent), "Bytes/Second"))
            data.append(m("net_bytes_recv_per_s",
                          (net_after.bytes_recv - net_before.bytes_recv), "Bytes/Second"))
    except Exception:
        pass

    # Load average
    la = os.getloadavg()
    data.append(m("load_avg_1m", la[0], "None"))

    # Procesos por servidor
    server = SERVER.lower()
    if "mediacap" in server or "mediadev" in server.replace("-", ""):
        data.append(m("ffmpeg_procs", proc_count("ffmpeg"), "Count"))
        data.append(m("stream_daemon_procs", proc_count("stream_daemon"), "Count"))
    else:
        data.append(m("media_app_procs", proc_count("media.app"), "Count"))
        data.append(m("nginx_procs", proc_count("nginx"), "Count"))

    return data

def push(data):
    # CloudWatch acepta máx 20 métricas por put
    for i in range(0, len(data), 20):
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=data[i:i+20])

def main():
    print(f"MediaDEV Metrics → namespace={NAMESPACE} server={SERVER} interval={INTERVAL}s",
          flush=True)
    while True:
        try:
            data = collect()
            push(data)
            print(f"[OK] {len(data)} métricas enviadas", flush=True)
        except Exception as e:
            print(f"[ERR] {e}", flush=True)
        time.sleep(INTERVAL - 2)  # -2 por el sleep de red

if __name__ == "__main__":
    main()
