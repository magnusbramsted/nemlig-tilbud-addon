#!/usr/bin/with-contenv bashio

INTERVAL="$(bashio::config 'update_interval_hours')"

if [ -z "${INTERVAL}" ]; then
  INTERVAL=24
fi

bashio::log.info "Nemlig tilbudshenter starter. Interval: ${INTERVAL} time(r)."

while true; do
  python3 /app/fetch_offers.py

  bashio::log.info "Næste tilbudshentning om ${INTERVAL} time(r)."
  sleep "$((INTERVAL * 3600))"
done
