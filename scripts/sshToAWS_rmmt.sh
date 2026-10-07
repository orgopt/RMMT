#!/bin/bash

ssh -t \
  -i "$HOME/.ssh/AWS KIS trading bot.pem" \
  ubuntu@52.79.221.73 \
  'cd /home/ubuntu/KISenv/Reverse_Momentum && exec bash -l'
