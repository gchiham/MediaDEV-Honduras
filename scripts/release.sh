#!/bin/bash
# Publicar nueva release del Destroyer a S3
# Uso: ./release.sh v11
set -e

VERSION="${1:?Uso: ./release.sh vX  (ej: ./release.sh v11)}"
RELEASE_NAME="destroyer-${VERSION}"

cd /opt/destroyer
set -a && source .env && set +a

echo "=== Empaquetando ${RELEASE_NAME} ==="
tar -czf /tmp/${RELEASE_NAME}.tar.gz worker.py fingerprint.py
ls -lh /tmp/${RELEASE_NAME}.tar.gz

echo "=== Subiendo a S3 ==="
export _REL="${RELEASE_NAME}"
/opt/destroyer/venv/bin/python3 -c "
import boto3, os
s3 = boto3.client('s3', region_name='us-east-1')
bucket = os.environ['S3_BUCKET']
name = os.environ['_REL']
s3.upload_file(f'/tmp/{name}.tar.gz', bucket, f'destroyer/releases/{name}.tar.gz')
s3.upload_file(f'/tmp/{name}.tar.gz', bucket, 'destroyer/releases/latest.tar.gz')
print(f'OK: s3://{bucket}/destroyer/releases/{name}.tar.gz')
print(f'OK: s3://{bucket}/destroyer/releases/latest.tar.gz (actualizado)')
"

echo ""
echo "=== Release ${RELEASE_NAME} publicada ==="
echo "Para activarla, editar /opt/destroyer/.env:"
echo "  DESTROYER_RELEASE=${RELEASE_NAME}"
