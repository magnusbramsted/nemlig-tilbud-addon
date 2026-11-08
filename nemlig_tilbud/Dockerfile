ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache \
    chromium \
    chromium-chromedriver \
    python3 \
    py3-pip \
    font-noto \
    font-noto-extra \
    nss \
    freetype \
    harfbuzz \
    ttf-freefont \
    && pip3 install --no-cache-dir --break-system-packages selenium

WORKDIR /app

COPY app/fetch_offers.py /app/fetch_offers.py
COPY app/import_mealie_opskrifter.py /app/import_mealie_opskrifter.py
COPY app/match_mealie_tilbud.py /app/match_mealie_tilbud.py
COPY app/fetch_nemlig_ny_side_test.py /app/fetch_nemlig_ny_side_test.py
COPY app/fetch_nemlig_ny_side.py /app/fetch_nemlig_ny_side.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD ["/run.sh"]
