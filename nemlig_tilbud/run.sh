#!/usr/bin/with-contenv bashio

INTERVAL="$(bashio::config 'update_interval_hours')"
if [ -z "${INTERVAL}" ] || [ "${INTERVAL}" = "null" ]; then
  INTERVAL=24
fi

bashio::log.info "Nemlig tilbudshenter starter. Interval: ${INTERVAL} time(r)."

# Kurv og leveringstider: lille HTTP-server til HA (port 8099, kun internt netværk).
(
  while true; do
    python3 /app/server.py
    bashio::log.warning "Kurv-serveren stoppede. Genstarter om 10 s."
    sleep 10
  done
) &

while true; do
  python3 /app/main.py
  bashio::log.info "Næste kørsel om ${INTERVAL} time(r)."
  sleep "$((INTERVAL * 3600))"
done
