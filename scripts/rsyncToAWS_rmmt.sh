#!/bin/bash

set -euo pipefail

#PROJECT_DIR="/Users/mikepark/Library/Mobile Documents/com~apple~CloudDocs/Codes/Stocks/KIS/BCSO_strategy"
PROJECT_DIR="/Users/mikepark/googleDrive/Codes/Stocks/KIS/Reverse_Momentum"
REMOTE_DIR="/home/ubuntu/KISenv/Reverse_Momentum/"

if ! compgen -G "${PROJECT_DIR}/main_rmmt*" > /dev/null; then
    echo "프로젝트 루트를 찾을 수 없습니다: ${PROJECT_DIR}" >&2
    exit 1
fi

rsync -av --delete \
  --exclude='*.log' \
  --exclude='__pycache__/' \
  --exclude='.DS_Store' \
  --exclude='Etc/' \
  --exclude='/rmmt/data/' \
  --exclude='/rmmt/logs/' \
  --exclude='/rmmt/results/' \
  --exclude='/.git/' \
  -e 'ssh -i "/Users/mikepark/.ssh/AWS KIS trading bot.pem"' \
  "${PROJECT_DIR}/" \
  "ubuntu@52.79.221.73:${REMOTE_DIR}"
