#!/bin/bash

ssh -t -i "~/.ssh/AWS KIS trading bot.pem" \
  ubuntu@52.79.221.73 \
  "tmux attach -t rmmt"
