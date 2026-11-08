ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache \
    chromium \
    chromium-chromedriver \
    python3 \
    py3-pip \
    font-noto \
    nss \
    freetype \
    harfbuzz \
    ttf-freefont \
    && pip3 install --no-cache-dir --break-system-packages selenium

WORKDIR /app
COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
