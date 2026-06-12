#!/usr/bin/env bash

# Logs:    journalctl --user -u german-teacher-bot.service -f
# Stop:    systemctl --user stop german-teacher-bot.service
# Disable: systemctl --user disable --now german-teacher-bot.service

systemctl --user restart german-teacher-bot.service
